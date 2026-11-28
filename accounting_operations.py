from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json

from flask import has_request_context, request
from sqlalchemy import event, func, inspect, select


def install_operations(service):
    from accounting import AccountingError, MAX_MINOR, parse_money

    db, ns = service.db, service.ns
    Product, Branch, Debt = (ns[name] for name in ('Product', 'Branch', 'Debt'))
    Warehouse, PO, POLine = (ns[name] for name in ('WarehouseInventory', 'PurchaseOrder', 'PurchaseOrderItem'))
    Invoice, Sale, SaleItem = (ns[name] for name in ('PurchaseInvoice', 'Sale', 'SaleItem'))

    class AccountingLayer(db.Model):
        __tablename__ = 'accounting_layer'
        id = db.Column(db.Integer, primary_key=True)
        location_id = db.Column(db.Integer, db.ForeignKey('accounting_location.id'), nullable=False, index=True)
        product_id = db.Column(db.Integer, nullable=False, index=True)
        warehouse_id = db.Column(db.Integer, index=True)
        source_key = db.Column(db.String(180), nullable=False, unique=True)
        received_at = db.Column(db.DateTime, nullable=False)
        quantity = db.Column(db.Integer, nullable=False)
        remaining = db.Column(db.Integer, nullable=False)
        value_minor = db.Column(db.BigInteger, nullable=False)
        remaining_minor = db.Column(db.BigInteger, nullable=False)
        __table_args__ = (db.CheckConstraint('quantity > 0 AND remaining >= 0 AND remaining <= quantity'),
                          db.CheckConstraint('value_minor >= 0 AND remaining_minor >= 0 AND remaining_minor <= value_minor'),)

    class AccountingAllocation(db.Model):
        __tablename__ = 'accounting_allocation'
        id = db.Column(db.Integer, primary_key=True)
        sale_item_id = db.Column(db.Integer, nullable=False, index=True)
        layer_id = db.Column(db.Integer, db.ForeignKey('accounting_layer.id'), nullable=False)
        quantity = db.Column(db.Integer, nullable=False)
        value_minor = db.Column(db.BigInteger, nullable=False)
        returned = db.Column(db.Integer, nullable=False, default=0)
        returned_minor = db.Column(db.BigInteger, nullable=False, default=0)
        __table_args__ = (db.CheckConstraint('quantity > 0 AND returned >= 0 AND returned <= quantity'),)

    class AccountingOperation(db.Model):
        __tablename__ = 'accounting_operation'
        key = db.Column(db.String(180), primary_key=True)
        fingerprint = db.Column(db.String(64), nullable=False)
        result = db.Column(db.JSON, nullable=False)

    service.Layer, service.Allocation, service.Operation = AccountingLayer, AccountingAllocation, AccountingOperation
    Layer, Allocation = AccountingLayer, AccountingAllocation

    def money(value):
        return f'{Decimal(value) / 100:.2f}'

    def integer(value, label, signed=False):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise AccountingError(f'{label} must be a whole number')
        try:
            number = int(value)
        except (ValueError, TypeError):
            raise AccountingError(f'{label} must be a whole number') from None
        if str(number) != str(value) or not (-2147483647 if signed else 1) <= number <= 2147483647 or number == 0:
            raise AccountingError(f'Invalid {label.lower()}')
        return number

    def value_for(quantity, unit_cost):
        try:
            cost = Decimal(str(unit_cost))
            if not cost.is_finite() or cost < 0 or cost > Decimal('999999999999.99'):
                raise ValueError()
            value = int((cost * quantity * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            if abs(value) > MAX_MINOR:
                raise AccountingError('Stock value exceeds the supported money limit')
            return value
        except (InvalidOperation, ValueError, TypeError):
            raise AccountingError('Every stock item needs a finite, non-negative reviewed cost') from None

    def portion(total, quantity, whole):
        return int((Decimal(total) * quantity / whole).quantize(Decimal('1'), rounding=ROUND_HALF_UP))

    def line(location, account, amount, reference=None):
        return {'location_id': location, 'account_code': account, 'amount_minor': amount, 'reference': reference}

    def remember_stock(product_id=None, warehouse_id=None, debt_id=None):
        if product_id:
            db.session.info.setdefault('_accounting_products', set()).add(product_id)
        if warehouse_id:
            db.session.info.setdefault('_accounting_warehouse', set()).add(warehouse_id)
        if debt_id:
            db.session.info.setdefault('_accounting_debts', set()).add(debt_id)

    def require_active():
        config = service.config()
        if config is None:
            raise AccountingError('Review and activate opening balances first', 409)
        return config

    def posted(key):
        return service.Journal.query.filter_by(source_key=key).first()

    def mark_stock_change(obj):
        db.session.info.setdefault('_accounting_stock_changes', set()).add(id(obj))

    def mark_receiving_change(obj, fields):
        db.session.info.setdefault('_accounting_receiving_changes', {})[id(obj)] = set(fields)

    @contextmanager
    def stock_mutation():
        session = db.session()
        previous = session.info.get('_accounting_stock_mutation', False)
        session.info['_accounting_stock_mutation'] = True
        try:
            yield
        finally:
            if previous:
                session.info['_accounting_stock_mutation'] = previous
            else:
                session.info.pop('_accounting_stock_mutation', None)

    def add_layer(location, product_id, quantity, amount, key, warehouse_id=None, received_at=None):
        row = Layer(location_id=location, product_id=product_id, warehouse_id=warehouse_id,
                    source_key=key, quantity=quantity, remaining=quantity,
                    value_minor=amount, remaining_minor=amount,
                    received_at=received_at or datetime.utcnow())
        service._add_immutable(row)
        remember_stock(warehouse_id=warehouse_id) if warehouse_id else remember_stock(product_id=product_id)
        return row

    def consume(location, product_id, quantity, warehouse_id=None, batch_number=None):
        query = Layer.query.filter_by(location_id=location, product_id=product_id).filter(Layer.remaining > 0)
        if warehouse_id is not None:
            query = query.filter_by(warehouse_id=warehouse_id)
        if batch_number:
            query = query.filter(Layer.warehouse_id.in_(select(Warehouse.id).where(Warehouse.batch_number == batch_number)))
        remaining, result = quantity, []
        for layer in query.order_by(Layer.received_at, Layer.id).all():
            take = min(remaining, layer.remaining)
            # Cumulative rounding assigns the final cent exactly once across partial issues.
            used = layer.quantity - layer.remaining
            amount = portion(layer.value_minor, used + take, layer.quantity) - portion(layer.value_minor, used, layer.quantity)
            service._mutate(layer, remaining=layer.remaining - take,
                            remaining_minor=layer.remaining_minor - amount)
            result.append((layer, take, amount))
            remember_stock(warehouse_id=layer.warehouse_id) if layer.warehouse_id else remember_stock(product_id=product_id)
            remaining -= take
            if not remaining:
                break
        if remaining:
            raise AccountingError('Stock has no sufficient reviewed FIFO cost layers. Reconcile inventory before continuing.', 409)
        return result

    def operation(data, prefix):
        if not isinstance(data, dict):
            raise AccountingError('A JSON object is required')
        key = data.get('key')
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise AccountingError('A request key of 1–128 characters is required')
        key = prefix + ':' + key
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        previous = db.session.get(AccountingOperation, key)
        if previous:
            if previous.fingerprint != fingerprint:
                raise AccountingError('Request key was already used for different data', 409)
            return key, fingerprint, previous.result
        return key, fingerprint, None

    def save_operation(key, fingerprint, result):
        service._add_immutable(AccountingOperation(key=key, fingerprint=fingerprint, result=result))
        return result

    def opening_preview():
        locations, warnings, unvalued = [], [], []
        for branch in Branch.query.order_by(Branch.id).all():
            quantity, value = 0, 0
            for product in Product.query.filter_by(branch_id=branch.id).filter(Product.stock != 0).yield_per(500):
                if product.stock < 0:
                    warnings.append(f'Negative stock: {product.name} ({branch.name})')
                quantity += product.stock
                if product.cost is None:
                    unvalued.append({'product_id': product.id, 'name': product.name, 'quantity': product.stock, 'location': branch.name})
                else:
                    value += value_for(product.stock, product.cost)
            ar = sum(parse_money(str(debt.balance)) for debt in Debt.query.filter_by(branch_id=branch.id).filter(Debt.balance > 0).yield_per(500))
            locations.append({'branch_id': branch.id, 'name': branch.name,
                              'stock_quantity': quantity, 'stock_value': money(value), 'receivables': money(ar)})
        quantity, value = 0, 0
        for batch in Warehouse.query.filter(Warehouse.quantity != 0).yield_per(500):
            if batch.quantity < 0:
                warnings.append(f'Negative warehouse quantity: batch {batch.id}')
            quantity += batch.quantity
            if batch.unit_cost is None:
                unvalued.append({'product_id': batch.product_id, 'name': f'Batch {batch.id}', 'quantity': batch.quantity, 'location': 'Central Warehouse'})
            else:
                value += value_for(batch.quantity, batch.unit_cost)
        unbilled = 0
        for item in POLine.query.join(PO).filter(POLine.received_qty > 0,
                ~PO.id.in_(select(Invoice.purchase_order_id))).yield_per(500):
            unbilled += value_for(item.received_qty, item.unit_cost)
        if Product.query.filter(Product.stock > 0, Product.branch_id.is_(None)).first():
            warnings.append('Stock without a shop must be assigned before activation.')
        if Debt.query.filter(Debt.balance > 0, Debt.branch_id.is_(None)).first():
            warnings.append('Receivables without a shop must be assigned before activation.')
        warnings.append('Synchronize all offline checkouts and complete or reconcile pending payment captures before activation.')
        return {'date': service.today().isoformat(), 'currency': ns['get_currency_code'](),
                'locations': locations, 'warehouse': {'quantity': quantity, 'value': money(value)},
                'unbilled_receipts': money(unbilled), 'unvalued_stock': unvalued[:100], 'warnings': warnings}

    def activate(data):
        if service.config():
            raise AccountingError('Accounting has already been activated', 409)
        if not isinstance(data, dict) or data.get('confirm') is not True or data.get('stock_costs_reviewed') is not True:
            raise AccountingError('Explicitly confirm reviewed opening balances and inventory costs')
        preview = opening_preview()
        if preview['unvalued_stock'] or len(preview['warnings']) > 1:
            raise AccountingError('Resolve missing costs, negative stock and unassigned balances before activation', 409)
        pending = ns.get('PaymentIntent')
        if pending is not None and pending.query.filter(pending.status == 'SUCCESS', pending.sale_transaction_id.is_(None)).first():
            raise AccountingError('Complete or reconcile captured payments before accounting activation', 409)
        today = service.today()
        db.session.info['_accounting_setup'] = True
        service._add_immutable(service.Config(id=1, currency=preview['currency'], start_date=today, activated_at=datetime.utcnow()))
        central = service.Location(name='Central Warehouse / Head Office', kind='warehouse', branch_id=None)
        service._add_immutable(central)
        branches = Branch.query.order_by(Branch.id).all()
        for branch in branches:
            service._add_immutable(service.Location(name=branch.name, kind='branch', branch_id=branch.id))
        db.session.flush()
        entries = []
        for product in Product.query.filter(Product.stock > 0).yield_per(500):
            location = service.location(product.branch_id)
            value = value_for(product.stock, product.cost)
            add_layer(location.id, product.id, product.stock, value, f'opening:product:{product.id}')
            entries.append(line(location.id, 'INVENTORY', value))
        for batch in Warehouse.query.filter(Warehouse.quantity > 0).yield_per(500):
            value = value_for(batch.quantity, batch.unit_cost)
            add_layer(central.id, batch.product_id, batch.quantity, value, f'opening:batch:{batch.id}',
                      warehouse_id=batch.id, received_at=batch.received_date)
            entries.append(line(central.id, 'INVENTORY', value))
        for debt in Debt.query.filter(Debt.balance > 0).yield_per(500):
            location = service.location(debt.branch_id)
            entries.append(line(location.id, 'AR', parse_money(str(debt.balance)), f'debt:{debt.id}'))
        for item in POLine.query.join(PO).filter(POLine.received_qty > 0,
                ~PO.id.in_(select(Invoice.purchase_order_id))).yield_per(500):
            entries.append(line(central.id, 'GRNI', -value_for(item.received_qty, item.unit_cost), f'po:{item.purchase_order_id}'))
        balances = data.get('opening_balances', [])
        if not isinstance(balances, list) or len(balances) > 1000:
            raise AccountingError('At most 1,000 opening balance lines are allowed')
        permitted = {'CASH', 'BANK', 'TAX_PAYABLE', 'CAPITAL'}
        for balance in balances:
            if not isinstance(balance, dict) or balance.get('account_code') not in permitted:
                raise AccountingError('Opening cash, bank, tax and capital lines only; stock and receivables are reconciled separately')
            branch_id = balance.get('branch_id')
            location = service.location(integer(branch_id, 'Branch') if branch_id is not None else None)
            debit = parse_money(balance.get('debit') or '0')
            credit = parse_money(balance.get('credit') or '0')
            if debit and credit:
                raise AccountingError('Enter a debit or credit, not both')
            entries.append(line(location.id, balance['account_code'], debit - credit))
        payables = data.get('payables', [])
        if not isinstance(payables, list) or len(payables) > 1000:
            raise AccountingError('At most 1,000 opening supplier bills are allowed')
        for row in payables:
            supplier_id = integer(row.get('supplier_id'), 'Supplier')
            supplier = db.session.get(ns['Supplier'], supplier_id)
            if not supplier:
                raise AccountingError('Opening payable supplier not found')
            reference = row.get('reference')
            if not isinstance(reference, str) or not 1 <= len(reference.strip()) <= 120:
                raise AccountingError('An opening supplier reference is required')
            amount = parse_money(row.get('amount'))
            if amount <= 0:
                raise AccountingError('Opening payable must be positive')
            invoice = Invoice.query.filter_by(supplier_id=supplier_id, supplier_reference=reference.strip()).first()
            source = f'purchase-invoice:{invoice.purchase_order_id}' if invoice else f'opening-payable:{supplier_id}:{reference.strip()}'
            payable = service.Payable(location_id=central.id, supplier_id=supplier_id, reference=reference.strip(),
                                      invoice_date=invoice.invoice_date if invoice else today,
                                      due_date=service.external_date(row['due_date']) if row.get('due_date') else None,
                                      total_minor=amount, balance_minor=amount, source_key=source)
            service._add_immutable(payable)
            db.session.flush()
            entries.append(line(central.id, 'AP', -amount, f'payable:{payable.id}'))
        grouped = defaultdict(list)
        for entry in entries:
            if entry['amount_minor']:
                grouped[entry['location_id']].append(entry)
        for location_id in sorted(grouped):
            batches, chunk, total = [], [], 0
            for entry in grouped[location_id]:
                amount = entry['amount_minor']
                if chunk and (len(chunk) == 999 or abs(total + amount) > MAX_MINOR):
                    batches.append((chunk, total))
                    chunk, total = [], 0
                chunk.append(entry)
                total += amount
                if len(chunk) == 999 or abs(total) > MAX_MINOR:
                    batches.append((chunk, total))
                    chunk, total = [], 0
            if chunk:
                batches.append((chunk, total))
            for part, (chunk, total) in enumerate(batches, 1):
                service.post(f'opening:{location_id}:{part}', 'opening', today,
                             'Reviewed opening balances; no historical profit reconstructed',
                             [*chunk, line(location_id, 'OPENING_EQUITY', -total)])
        db.session.flush()
        return {'success': True, 'start_date': today.isoformat(), 'currency': preview['currency']}

    def inventory_report(location_id=None):
        require_active()
        query = db.session.query(Layer.location_id, Layer.product_id,
            func.sum(Layer.remaining), func.sum(Layer.remaining_minor)).filter(Layer.remaining > 0)
        if location_id is not None:
            query = query.filter(Layer.location_id == location_id)
        groups = query.group_by(Layer.location_id, Layer.product_id).order_by(Layer.location_id, Layer.product_id)
        rows = groups.limit(1001).all()
        if len(rows) > 1000:
            raise AccountingError('Select a shop or narrow inventory before requesting more than 1,000 costed items', 413)
        products = {p.id: p.name for p in Product.query.filter(Product.id.in_([r[1] for r in rows])).all()}
        locations = {location.id: location.name for location in service.Location.query.all()}
        items = [{'location_id': loc, 'location_name': locations[loc], 'product_id': product,
                  'product_name': products.get(product, f'Archived product {product}'), 'quantity': qty, 'value': money(value)}
                 for loc, product, qty, value in rows]
        return {'items': items, 'total_value': money(sum(row[3] for row in rows))}

    def post_receipt(receipt):
        if not service.config() or posted(f'receipt:{receipt.id}'):
            return
        location = service.location(None)
        total = 0
        for item in receipt.snapshot['items']:
            quantity = item['received_qty']
            value = value_for(quantity, item['unit_cost'])
            total += value
            add_layer(location.id, item['product_id'], quantity, value,
                      f'receipt:{receipt.id}:{item["warehouse_inventory_id"]}',
                      warehouse_id=item['warehouse_inventory_id'], received_at=receipt.created_at)
        if total:
            service.post(f'receipt:{receipt.id}', 'receipt', service.today(), f'Goods receipt {receipt.number}', [
                line(location.id, 'INVENTORY', total), line(location.id, 'GRNI', -total, f'po:{receipt.purchase_order_id}')])

    def post_invoice(invoice):
        if not service.config():
            return
        key = f'purchase-invoice:{invoice.purchase_order_id}'
        if posted(key):
            return
        if invoice.currency != service.config().currency:
            raise AccountingError('Supplier invoice currency differs from the accounting book', 409)
        central = service.location(None)
        amount = invoice.total_minor
        accrued = -(db.session.query(func.coalesce(func.sum(service.Line.amount_minor), 0)).filter(
            service.Line.account_code == 'GRNI', service.Line.reference == f'po:{invoice.purchase_order_id}').scalar() or 0)
        if accrued != amount:
            raise AccountingError('Goods-received accrual does not match this invoice. Reconcile the opening receipt first.', 409)
        payable = service.Payable(location_id=central.id, supplier_id=invoice.supplier_id,
            reference=invoice.supplier_reference, invoice_date=invoice.invoice_date,
            total_minor=amount, balance_minor=amount, source_key=key)
        service._add_immutable(payable)
        db.session.flush()
        if amount:
            service.post(key, 'purchase-invoice', invoice.invoice_date, f'Supplier invoice {invoice.supplier_reference}', [
                line(central.id, 'GRNI', amount, f'po:{invoice.purchase_order_id}'),
                line(central.id, 'AP', -amount, f'payable:{payable.id}')])
        service.document('purchase', key, central.id, invoice.invoice_date, amount,
            {**invoice.snapshot, 'supplier_reference': invoice.supplier_reference, 'currency': invoice.currency})

    def post_transfer(transfer):
        with db.session.no_autoflush:
            if not service.config() or posted(f'transfer:{transfer.id}'):
                return
            source, target = service.location(None), service.location(transfer.branch_id)
            if not transfer.target_product_id:
                raise AccountingError('A costed transfer needs a destination product', 409)
            allocations = consume(source.id, transfer.product_id, transfer.quantity, batch_number=transfer.batch_number)
            total = 0
            for layer, quantity, value in allocations:
                total += value
                if layer.warehouse_id:
                    batch = db.session.get(Warehouse, layer.warehouse_id)
                    if batch is not None:
                        mark_stock_change(batch)
                add_layer(target.id, transfer.target_product_id, quantity, value, f'transfer:{transfer.id}:{layer.id}', received_at=layer.received_at)
        remember_stock(product_id=transfer.target_product_id)
        if total:
            service.post(f'transfer:{transfer.id}', 'transfer', service.today(), f'Warehouse transfer {transfer.id}', [
                line(source.id, 'INTERBRANCH', total, f'transfer:{transfer.id}'), line(source.id, 'INVENTORY', -total),
                line(target.id, 'INVENTORY', total), line(target.id, 'INTERBRANCH', -total, f'transfer:{transfer.id}')])

    def sale_cost(sale):
        location, total = service.location(sale.branch_id), 0
        items = SaleItem.query.filter_by(sale_id=sale.id).order_by(SaleItem.id).all()
        for item in items:
            for layer, quantity, value in consume(location.id, item.product_id, item.quantity):
                total += value
                service._add_immutable(Allocation(sale_item_id=item.id, layer_id=layer.id, quantity=quantity,
                                                  value_minor=value, returned=0, returned_minor=0))
        return total

    def sale_document(sale, key):
        location = service.location(sale.branch_id)
        try:
            snapshot = json.loads(sale.receipt_snapshot) if sale.receipt_snapshot else {}
        except (ValueError, TypeError):
            snapshot = {}
        customer = db.session.get(ns['Customer'], sale.customer_id) if sale.customer_id else None
        snapshot.update(transaction_id=sale.transaction_id,
            customer={'name': customer.name, 'address': customer.address, 'phone': customer.phone} if customer else None)
        if not snapshot.get('items'):
            snapshot['items'] = [{'product_id': row.product_id, 'product_name': db.session.get(Product, row.product_id).name,
                                  'quantity': row.quantity, 'unit_price': str(row.price), 'tax': str(row.tax)}
                                 for row in SaleItem.query.filter_by(sale_id=sale.id).all()]
        service.document('sales', key, location.id, service.today(), parse_money(str(sale.total)), snapshot)

    def post_sale(sale):
        if not service.config() or posted(f'sale:{sale.id}'):
            return
        db.session.flush()
        location = service.location(sale.branch_id)
        total, tax = parse_money(str(sale.total)), parse_money(str(sale.tax))
        cost = sale_cost(sale)
        entries = [line(location.id, 'SALES', -(total - tax)), line(location.id, 'TAX_PAYABLE', -tax),
                   line(location.id, 'COGS', cost), line(location.id, 'INVENTORY', -cost)]
        if sale.payment_method == 'debt':
            debts = Debt.query.filter_by(sale_id=sale.id).all()
            if len(debts) != 1 or parse_money(str(debts[0].balance)) != total:
                raise AccountingError('The credit invoice must have one matching customer balance', 409)
            entries.append(line(location.id, 'AR', total, f'debt:{debts[0].id}'))
            remember_stock(debt_id=debts[0].id)
        elif sale.payment_method == 'split_payment':
            breakdown = json.loads(sale.payment_breakdown)
            for method, value in breakdown.items():
                entries.append(line(location.id, 'CASH' if method == 'cash' else 'BANK', parse_money(str(value))))
        else:
            entries.append(line(location.id, 'CASH' if sale.payment_method == 'cash' else 'BANK', total))
        if any(row['amount_minor'] for row in entries):
            service.post(f'sale:{sale.id}', 'sale', service.today(), f'Sale {sale.transaction_id}', entries)
        sale_document(sale, f'sale:{sale.id}')

    def post_collection(payment):
        if not service.config():
            return
        key = 'collection:' + str(getattr(payment, '_accounting_key', payment.id))
        if posted(key):
            return
        location = service.location(payment.branch_id)
        amount = parse_money(str(payment.amount))
        account = getattr(payment, '_accounting_account', 'CASH')
        if account not in ('CASH', 'BANK'):
            raise AccountingError('Choose cash or bank for the collection')
        service.post(key, 'collection', getattr(payment, '_accounting_date', service.today()), f'Customer collection for debt {payment.debt_id}', [
            line(location.id, account, amount), line(location.id, 'AR', -amount, f'debt:{payment.debt_id}')],
            actor_id=getattr(payment, '_accounting_actor_id', None))
        remember_stock(debt_id=payment.debt_id)

    def post_return(workflow, legacy_costs=None):
        if not service.config() or posted(f'return:{workflow.id}'):
            return
        db.session.flush()
        original = db.session.get(Sale, workflow.original_sale_id)
        location = service.location(original.branch_id)
        recovered, returned_tax = 0, 0
        rows = ns['ReturnExchangeItem'].query.filter_by(return_exchange_id=workflow.id, movement='return').order_by(ns['ReturnExchangeItem'].id).all()
        for item in rows:
            returned_tax += parse_money(str(item.line_tax))
            allocations = Allocation.query.filter_by(sale_item_id=item.original_sale_item_id).order_by(Allocation.id).all()
            remaining, value = item.quantity, 0
            if allocations:
                for allocation in allocations:
                    quantity = min(remaining, allocation.quantity - allocation.returned)
                    if not quantity:
                        continue
                    amount = portion(allocation.value_minor, allocation.returned + quantity, allocation.quantity) - allocation.returned_minor
                    service._mutate(allocation, returned=allocation.returned + quantity,
                                    returned_minor=allocation.returned_minor + amount)
                    value += amount
                    remaining -= quantity
                    if not remaining:
                        break
                if remaining:
                    raise AccountingError('Return exceeds the original cost allocation', 409)
            else:
                raw_cost = (legacy_costs or {}).get(str(item.original_sale_item_id)) if isinstance(legacy_costs, dict) else None
                if raw_cost is None:
                    raise AccountingError('This pre-accounting sale has no historical cost. Enter an accountant-reviewed recovery unit cost for each returned line.', 409)
                value = value_for(item.quantity, raw_cost)
            recovered += value
            add_layer(location.id, item.product_id, item.quantity, value, f'return:{workflow.id}:{item.id}')
        returned = parse_money(str(workflow.return_total))
        replacement = db.session.get(Sale, workflow.adjustment_sale_id) if workflow.adjustment_sale_id else None
        replacement_total = parse_money(str(workflow.exchange_total))
        replacement_tax = parse_money(str(replacement.tax)) if replacement else 0
        replacement_cost = sale_cost(replacement) if replacement else 0
        entries = [line(location.id, 'SALES_RETURNS', returned - returned_tax),
                   line(location.id, 'TAX_PAYABLE', returned_tax - replacement_tax),
                   line(location.id, 'SALES', -(replacement_total - replacement_tax)),
                   line(location.id, 'INVENTORY', recovered - replacement_cost),
                   line(location.id, 'COGS', replacement_cost - recovered)]
        credits = getattr(workflow, '_accounting_debt_credits', [])
        for credit in credits:
            entries.append(line(location.id, 'AR', -credit['amount_minor'], f'debt:{credit["debt_id"]}'))
            remember_stock(debt_id=credit['debt_id'])
        settlement = parse_money(str(workflow.collected_amount)) - parse_money(str(workflow.refund_amount))
        entries.append(line(location.id, 'CASH' if workflow.settlement_method == 'cash' else 'BANK', settlement))
        if any(row['amount_minor'] for row in entries):
            service.post(f'return:{workflow.id}', 'sales-return', service.today(), f'Return {workflow.workflow_id}', entries)
        service.document('sales-return', f'return:{workflow.id}', location.id, service.today(), returned,
            {'original_transaction_id': original.transaction_id, 'workflow_id': workflow.workflow_id,
             'items': [{'product_id': item.product_id, 'quantity': item.quantity, 'unit_price': str(item.unit_price),
                        'line_total': str(item.line_total), 'tax': str(item.line_tax)} for item in rows],
             'debt_credit': money(sum(row['amount_minor'] for row in credits)),
             'refund': str(workflow.refund_amount), 'collected': str(workflow.collected_amount),
             'notes': workflow.notes})
        if replacement:
            sale_document(replacement, f'sale:{replacement.id}')

    def adjust_inventory(data):
        require_active()
        key, fingerprint, previous = operation(data, 'stock-adjustment')
        if previous:
            return previous
        location = service.require_location(integer(data.get('location_id'), 'Location'), active=True)
        if location.kind != 'branch':
            raise AccountingError('Choose a shop for a stock adjustment')
        product = db.session.get(Product, integer(data.get('product_id'), 'Product'))
        if not product or product.branch_id != location.branch_id:
            raise AccountingError('Product does not belong to the selected shop')
        quantity = integer(data.get('quantity'), 'Quantity', signed=True)
        reason = data.get('reason')
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 300:
            raise AccountingError('Enter a stock-adjustment reason (3–300 characters)')
        date = service.external_date(data.get('date'))
        if quantity > 0:
            value = value_for(quantity, data.get('unit_cost'))
            add_layer(location.id, product.id, quantity, value, key)
        else:
            if product.stock < -quantity:
                raise AccountingError('Adjustment exceeds available stock')
            value = -sum(part[2] for part in consume(location.id, product.id, -quantity))
        product.stock += quantity
        if value:
            service.post(key, 'stock-adjustment', date, reason.strip(), [
                line(location.id, 'INVENTORY', value), line(location.id, 'INVENTORY_ADJUSTMENT', -value)])
        remember_stock(product_id=product.id)
        return save_operation(key, fingerprint, {'success': True, 'quantity': product.stock, 'value': money(value)})

    def purchase_return(data):
        require_active()
        key, fingerprint, previous = operation(data, 'purchase-return')
        if previous:
            return previous
        payable = db.session.get(service.Payable, integer(data.get('payable_id'), 'Supplier bill'))
        if not payable or not payable.source_key.startswith('purchase-invoice:'):
            raise AccountingError('Select a matched purchase invoice, not an expense bill')
        po_id = int(payable.source_key.split(':', 1)[1])
        invoice = db.session.get(Invoice, po_id)
        if not invoice:
            raise AccountingError('Original supplier invoice is unavailable', 409)
        items = data.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= 500:
            raise AccountingError('Select 1–500 warehouse batches')
        reason = data.get('reason')
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 300:
            raise AccountingError('Enter a supplier-return reason (3–300 characters)')
        central = service.location(None)
        valid_batches = set()
        for receipt in ns['GoodsReceipt'].query.filter_by(purchase_order_id=po_id).all():
            valid_batches.update(row['warehouse_inventory_id'] for row in receipt.snapshot['items'])
        seen, total, snapshot = set(), 0, []
        for item in items:
            if not isinstance(item, dict):
                raise AccountingError('Invalid supplier-return line')
            batch_id = integer(item.get('warehouse_inventory_id'), 'Warehouse batch')
            quantity = integer(item.get('quantity'), 'Quantity')
            if batch_id in seen:
                raise AccountingError('Select each warehouse batch only once')
            seen.add(batch_id)
            batch = db.session.get(Warehouse, batch_id)
            if batch_id not in valid_batches or not batch or batch.quantity < quantity:
                raise AccountingError('Return only remaining warehouse stock from the selected invoice', 409)
            parts = consume(central.id, batch.product_id, quantity, warehouse_id=batch.id)
            value = sum(row[2] for row in parts)
            total += value
            mark_stock_change(batch)
            batch.quantity -= quantity
            snapshot.append({'warehouse_inventory_id': batch.id, 'product_id': batch.product_id,
                             'quantity': quantity, 'line_total': money(value)})
        applied = min(total, payable.balance_minor)
        refund = total - applied
        paid_to = data.get('paid_to')
        if refund and paid_to not in ('CASH', 'BANK'):
            raise AccountingError('This return exceeds the unpaid bill. Choose CASH or BANK for the supplier refund.', 409)
        date = service.external_date(data.get('date'))
        if date < payable.invoice_date:
            raise AccountingError('Purchase return cannot precede the supplier invoice')
        service._mutate(payable, balance_minor=payable.balance_minor - applied)
        if total:
            service.post(key, 'purchase-return', date, reason.strip(), [
                line(central.id, 'AP', applied, f'payable:{payable.id}'),
                *([line(central.id, paid_to, refund, f'payable:{payable.id}')] if refund else []),
                line(central.id, 'INVENTORY', -total)])
        document = service.document('purchase-return', key, central.id, date, total,
            {'supplier_reference': invoice.supplier_reference, 'supplier_name': invoice.snapshot.get('supplier_name'),
             'items': snapshot, 'reason': reason.strip(), 'payable_credit': money(applied),
             'supplier_refund': money(refund), 'refund_account': paid_to if refund else None})
        db.session.flush()
        return save_operation(key, fingerprint, {'success': True, 'document_id': document.id, 'total': money(total)})

    def source_key(obj):
        name = type(obj).__name__
        if name == 'Sale':
            return f'sale:{obj.id}'
        if name == 'SaleItem':
            return f'sale:{obj.sale_id}'
        if name == 'PurchaseInvoice':
            return f'purchase-invoice:{obj.purchase_order_id}'
        if name == 'ReturnExchange':
            return f'return:{obj.id}'
        if name == 'ReturnExchangeItem':
            return f'return:{obj.return_exchange_id}'
        if name == 'GoodsReceipt':
            return f'receipt:{obj.id}'
        if name in ('PurchaseOrder', 'PurchaseOrderItem'):
            return f'po:{obj.id if name == "PurchaseOrder" else obj.purchase_order_id}'
        if name == 'WarehouseInventory':
            return f'warehouse:{obj.id}'
        if name == 'WarehouseTransfer':
            return f'transfer:{obj.id}'
        if name == 'DebtPayment':
            return f'debt-payment:{obj.id}'
        return None

    def source_keys(obj):
        keys = {source_key(obj)} - {None}
        parents = {'SaleItem': ('sale_id', 'sale'),
                   'PurchaseInvoice': ('purchase_order_id', 'purchase-invoice'),
                   'ReturnExchangeItem': ('return_exchange_id', 'return'),
                   'PurchaseOrderItem': ('purchase_order_id', 'po')}
        parent = parents.get(type(obj).__name__)
        if parent:
            attribute, prefix = parent
            for old in inspect(obj).attrs[attribute].history.deleted:
                keys.add(f'{prefix}:{old}')
        return keys

    def po_recorded(po_id):
        return (ns['GoodsReceipt'].query.filter_by(purchase_order_id=po_id).first() is not None
                or Invoice.query.filter_by(purchase_order_id=po_id).first() is not None
                or service.Line.query.filter_by(reference=f'po:{po_id}').first() is not None)

    def source_recorded(obj):
        name = type(obj).__name__
        if name in ('DebtPayment', 'WarehouseTransfer', 'GoodsReceipt'):
            return True
        keys = source_keys(obj)
        if name == 'WarehouseInventory':
            return Layer.query.filter_by(warehouse_id=obj.id).first() is not None
        if name in ('PurchaseOrder', 'PurchaseOrderItem'):
            return any(po_recorded(int(key.split(':', 1)[1])) for key in keys)
        return any(posted(key) or service.Document.query.filter_by(source_key=key).first() for key in keys)

    def authorized_source_change(obj, session):
        name = type(obj).__name__
        changed = {attribute.key for attribute in inspect(obj).attrs if attribute.history.has_changes()}
        if name == 'WarehouseInventory' and id(obj) in session.info.get('_accounting_stock_changes', set()):
            return changed <= {'quantity', 'updated_at'}
        if name in ('PurchaseOrder', 'PurchaseOrderItem') and id(obj) in session.info.get('_accounting_receiving_changes', {}):
            return changed <= session.info['_accounting_receiving_changes'][id(obj)]
        return False

    def before_flush(session, context, instances):
        if session.info.get('_accounting_setup') or not service.config():
            return
        with session.no_autoflush:
            for obj in list(session.deleted):
                if source_recorded(obj):
                    raise AccountingError('Posted financial records must be retained. Use a return or reversal instead of deleting history.', 409)
                if isinstance(obj, Branch):
                    location = service.Location.query.filter_by(branch_id=obj.id).first()
                    if location:
                        raise AccountingError('Shops with an accounting location must be deactivated, not deleted', 409)
                if isinstance(obj, Product) and Layer.query.filter_by(product_id=obj.id).first():
                    raise AccountingError('Products with accounting cost history must be retained', 409)
                if isinstance(obj, Debt) and service.Line.query.filter_by(reference=f'debt:{obj.id}', account_code='AR').first():
                    raise AccountingError('Posted receivables must be settled or credited, not deleted', 409)
                if isinstance(obj, ns['Supplier']) and service.Payable.query.filter_by(supplier_id=obj.id).first():
                    raise AccountingError('Suppliers with accounting history must be retained', 409)
            for obj in list(session.dirty):
                if source_recorded(obj) and not authorized_source_change(obj, session) and session.is_modified(obj, include_collections=False):
                    raise AccountingError('Posted financial source records cannot be edited', 409)
            for obj in list(session.new) + list(session.dirty):
                if isinstance(obj, Branch):
                    session.info.setdefault('_accounting_branches', {})[id(obj)] = obj
                elif isinstance(obj, Product):
                    if obj.id:
                        remember_stock(product_id=obj.id)
                    elif obj in session.new:
                        session.info.setdefault('_accounting_new_products', {})[id(obj)] = obj
                elif isinstance(obj, Warehouse):
                    if obj.id:
                        remember_stock(warehouse_id=obj.id)
                    elif obj in session.new:
                        session.info.setdefault('_accounting_new_warehouse', {})[id(obj)] = obj
                elif isinstance(obj, Debt):
                    if obj.id:
                        remember_stock(debt_id=obj.id)
                    elif obj in session.new:
                        session.info.setdefault('_accounting_new_debts', {})[id(obj)] = obj
                elif isinstance(obj, ns['DebtPayment']):
                    session.info.setdefault('_accounting_collections', {})[id(obj)] = obj

    def before_commit(session):
        if session.info.get('_accounting_checking') or session.info.get('_accounting_setup') or not service.config():
            return
        session.info['_accounting_checking'] = True
        try:
            session.flush()
            for branch in session.info.get('_accounting_branches', {}).values():
                location = service.Location.query.filter_by(branch_id=branch.id).first()
                if location is None:
                    service._add_immutable(service.Location(name=branch.name, kind='branch', branch_id=branch.id))
                elif location.name != branch.name:
                    service._mutate(location, name=branch.name)
            for key in ('_accounting_new_products', '_accounting_new_warehouse', '_accounting_new_debts'):
                for obj in session.info.get(key, {}).values():
                    if isinstance(obj, Product):
                        remember_stock(product_id=obj.id)
                    elif isinstance(obj, Warehouse):
                        remember_stock(warehouse_id=obj.id)
                    elif isinstance(obj, Debt):
                        remember_stock(debt_id=obj.id)
            session.flush()
            # Collection posting is explicit in the new API; this also covers legacy callers.
            for payment in session.info.get('_accounting_collections', {}).values():
                post_collection(payment)
            session.flush()
            if ns['get_currency_code']() != service.config().currency:
                raise AccountingError('The accounting book currency cannot be changed after activation', 409)
            for product_id in session.info.get('_accounting_products', set()):
                product = session.get(Product, product_id)
                if product is None:
                    continue
                location = service.location(product.branch_id)
                qty = session.query(func.coalesce(func.sum(Layer.remaining), 0)).filter_by(
                    product_id=product_id, location_id=location.id).scalar()
                actual = session.query(Product.stock).filter_by(id=product_id).scalar() or 0
                if qty != actual:
                    raise AccountingError('Stock changes must use a costed sale, return, transfer or Accounting inventory adjustment', 409)
            for warehouse_id in session.info.get('_accounting_warehouse', set()):
                qty = session.query(func.coalesce(func.sum(Layer.remaining), 0)).filter_by(warehouse_id=warehouse_id).scalar()
                actual = session.query(Warehouse.quantity).filter_by(id=warehouse_id).scalar() or 0
                if qty != actual:
                    raise AccountingError('Warehouse stock and accounting FIFO layers do not reconcile', 409)
            for debt_id in session.info.get('_accounting_debts', set()):
                debt = session.get(Debt, debt_id)
                balance = session.query(func.coalesce(func.sum(service.Line.amount_minor), 0)).filter_by(
                    account_code='AR', reference=f'debt:{debt_id}').scalar()
                if debt is None or parse_money(str(debt.balance)) != balance:
                    raise AccountingError('Customer balances must be changed through a posted collection or credit note', 409)
        finally:
            session.info.pop('_accounting_checking', None)

    def bulk_guard(state):
        if not (state.is_update or state.is_delete) or state.bind_mapper is None or not service.config():
            return
        model = state.bind_mapper.class_
        transfer_model = ns.get('WarehouseTransfer')
        guarded = (Product, Warehouse, Debt, Sale, SaleItem, Invoice, PO, POLine,
                   ns['GoodsReceipt'], ns['ReturnExchange'], ns['ReturnExchangeItem'], ns['DebtPayment'])
        if model not in guarded and model is not transfer_model:
            return
        statement = state.statement
        with state.session.no_autoflush:
            rows = state.session.query(model).filter(*statement._where_criteria).limit(10001).all()
            if len(rows) > 10000:
                raise AccountingError('Financial changes must be processed in bounded batches', 413)
            values = getattr(statement, '_values', {})
            customer_unlink = (state.is_update and model is Sale and len(values) == 1 and all(
                getattr(column, 'name', None) == 'customer_id' and getattr(value, 'value', value) is None
                for column, value in values.items()))
            if model in (Sale, SaleItem, Invoice, ns['GoodsReceipt'], ns['ReturnExchange'],
                         ns['ReturnExchangeItem'], ns['DebtPayment'], transfer_model):
                if rows and not customer_unlink:
                    raise AccountingError('Use the accounting document workflow instead of bulk financial edits', 409)
            for obj in rows:
                if model is Product:
                    if state.is_delete and Layer.query.filter_by(product_id=obj.id).first():
                        raise AccountingError('Products with accounting history must be retained', 409)
                    remember_stock(product_id=obj.id)
                elif model is Warehouse:
                    if not state.session.info.get('_accounting_stock_mutation') and Layer.query.filter_by(warehouse_id=obj.id).first():
                        raise AccountingError('Costed warehouse batches must be changed through a costed transfer or supplier return', 409)
                    remember_stock(warehouse_id=obj.id)
                elif model is Debt:
                    remember_stock(debt_id=obj.id)
                elif model in (PO, POLine) and source_recorded(obj):
                    raise AccountingError('Received purchase records must be retained; record another receipt instead', 409)

    def clear_state(session, *args):
        for key in tuple(session.info):
            if key.startswith('_accounting_'):
                session.info.pop(key, None)

    target = db.session.session_factory
    event.listen(target, 'before_flush', before_flush)
    event.listen(target, 'before_commit', before_commit)
    event.listen(target, 'do_orm_execute', bulk_guard)
    event.listen(target, 'after_commit', clear_state)
    event.listen(target, 'after_rollback', clear_state)
    service.opening_preview = opening_preview
    service.activate = activate
    service.inventory_report = inventory_report
    service.adjust_inventory = adjust_inventory
    service.purchase_return = purchase_return
    service.post_receipt = post_receipt
    service.post_invoice = post_invoice
    service.post_transfer = post_transfer
    service.post_sale = post_sale
    service.post_return = post_return
    service.post_collection = post_collection
    service.stock_mutation = stock_mutation
    service.mark_receiving_change = mark_receiving_change
