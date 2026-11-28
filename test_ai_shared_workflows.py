"""Shared AI workflows against real models in an isolated, disposable instance."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from flask import Flask, session

from ai_tools import AITools, TOOL_METADATA


class SharedAIWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='pos-ai-shared-')
        cls.root = Path(cls.temporary.name)
        (cls.root / 'instance').mkdir()
        original_init = Flask.__init__

        def isolated_init(app, *args, **kwargs):
            kwargs.update(instance_path=str(cls.root / 'instance'), root_path=str(cls.root))
            original_init(app, *args, **kwargs)

        name = '_isolated_ai_shared_app'
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('app.py'))
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[name] = cls.module
        with patch.object(Flask, '__init__', isolated_init):
            spec.loader.exec_module(cls.module)
        cls.app = cls.module.app
        cls.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.module.db.session.remove()
            cls.module.db.engine.dispose()
        sys.modules.pop('_isolated_ai_shared_app', None)
        cls.temporary.cleanup()

    def setUp(self):
        m = self.module
        self.context = self.app.app_context()
        self.context.push()
        m.db.drop_all()
        m.db.create_all()
        a = m.Branch(name='Alpha', code='A', is_active=True, is_default=True)
        b = m.Branch(name='Beta', code='B', is_active=True)
        m.db.session.add_all([a, b])
        m.db.session.flush()
        self.a, self.b = a.id, b.id
        local_category = m.Category(name='Common', branch_id=a.id)
        foreign_category = m.Category(name='Common', branch_id=b.id)
        unique_category = m.Category(name='Beta only', branch_id=b.id)
        user = m.User(username='manager', password='unused', role='manager', branch_id=a.id)
        m.db.session.add_all([local_category, foreign_category, unique_category, user])
        m.db.session.flush()
        self.ca, self.cb, self.unique, self.user = (
            local_category.id, foreign_category.id, unique_category.id, user.id)
        local = m.Product(name='Local', barcode='LOCAL', stock=4, price=10, cost=2, branch_id=a.id)
        foreign = m.Product(name='Other', barcode='PHYSICAL', stock=8, price=20, cost=3,
                            category='Beta only', category_id=self.unique, branch_id=b.id)
        supplier = m.Supplier(name='Shared supplier', branch_id=b.id)
        customer_a = m.Customer(name='Local customer', branch_id=a.id)
        customer_b = m.Customer(name='Private customer', branch_id=b.id)
        m.db.session.add_all([local, foreign, supplier, customer_a, customer_b])
        m.db.session.flush()
        self.local, self.foreign, self.supplier = local.id, foreign.id, supplier.id
        self.customer = customer_b.id
        debt_a = m.Debt(customer_id=customer_a.id, amount=10, balance=10, branch_id=a.id)
        debt_b = m.Debt(customer_id=customer_b.id, amount=20, balance=20, branch_id=b.id)
        po = m.PurchaseOrder(po_number='B-PO', supplier_id=supplier.id, branch_id=b.id, status='pending')
        m.db.session.add_all([debt_a, debt_b, po])
        m.db.session.flush()
        line = m.PurchaseOrderItem(purchase_order_id=po.id, product_id=foreign.id,
                                   ordered_qty=5, received_qty=0, unit_cost=6)
        m.db.session.add_all([line, m.WarehouseInventory(product_id=foreign.id, quantity=10,
                            unit_cost=3, branch_id=b.id, batch_number='OLD')])
        m.db.session.commit()
        self.po, self.line, self.debt = po.id, line.id, debt_b.id
        self.debt_a = debt_a.id
        self.tools = AITools(m.db, m.AI_MODELS)
        self.tools.set_context(dict(user_id=self.user, role='manager', branch_id=self.a))

    def tearDown(self):
        self.module.db.session.remove()
        self.context.pop()

    def activate_accounting(self):
        m = self.module
        m.accounting.write_lock()
        m.accounting.activate({'confirm': True, 'stock_costs_reviewed': True,
                               'opening_balances': [], 'payables': []})
        m.db.session.commit()

    def test_customer_cleanup_unlinks_posted_sales_but_retains_debt_history(self):
        m = self.module
        settled = m.Customer(name='Settled customer', branch_id=self.a)
        m.db.session.add(settled)
        m.db.session.flush()
        settled_debt = m.Debt(customer_id=settled.id, amount=10, balance=0,
                              status='paid', branch_id=self.a)
        m.db.session.add(settled_debt)
        m.db.session.flush()
        payment = m.DebtPayment(debt_id=settled_debt.id, customer_id=settled.id,
                                amount=10, branch_id=self.a, processed_by=self.user)
        m.db.session.add(payment)
        m.db.session.commit()
        self.activate_accounting()

        sales = []
        for name in ('HTTP cleanup', 'AI cleanup'):
            customer = m.Customer(name=name, branch_id=self.a)
            m.db.session.add(customer)
            m.db.session.flush()
            sale = m.Sale(transaction_id=name, branch_id=self.a, customer_id=customer.id,
                          user_id=self.user, total=10, tax=0, payment_method='cash',
                          receipt_snapshot='{"items": []}')
            m.db.session.add(sale)
            m.db.session.flush()
            m.accounting.post_sale(sale)
            sales.append((customer.id, sale.id, name))
        m.db.session.commit()

        client = self.app.test_client()
        with client.session_transaction() as current:
            current.update(user_id=self.user, username='manager', role='manager', branch_id=self.a)
        http_customer, http_sale, http_name = sales[0]
        response = client.delete(f'/api/customers/{http_customer}')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIsNone(m.db.session.get(m.Customer, http_customer))
        self.assertIsNone(m.db.session.get(m.Sale, http_sale).customer_id)
        document = m.accounting.Document.query.filter_by(source_key=f'sale:{http_sale}').one()
        self.assertEqual(document.snapshot['customer']['name'], http_name)

        ai_customer, ai_sale, ai_name = sales[1]
        result = self.tools.delete_customer(ai_customer)
        self.assertTrue(result['success'], result)
        self.assertIsNone(m.db.session.get(m.Customer, ai_customer))
        self.assertIsNone(m.db.session.get(m.Sale, ai_sale).customer_id)
        document = m.accounting.Document.query.filter_by(source_key=f'sale:{ai_sale}').one()
        self.assertEqual(document.snapshot['customer']['name'], ai_name)

        blocked = client.delete(f'/api/customers/{settled.id}')
        self.assertEqual(blocked.status_code, 409, blocked.get_data(as_text=True))
        self.assertIn('history', blocked.json['message'])
        self.assertIn('history', self.tools.delete_customer(settled.id)['error'])
        self.assertIsNotNone(m.db.session.get(m.Customer, settled.id))
        self.assertIsNotNone(m.db.session.get(m.Debt, settled_debt.id))
        self.assertIsNotNone(m.db.session.get(m.DebtPayment, payment.id))

    def test_active_ledger_http_product_create_edit_and_stock_adjustments(self):
        m = self.module
        self.activate_accounting()
        client = self.app.test_client()
        with client.session_transaction() as current:
            current.update(user_id=self.user, username='manager', role='manager', branch_id=self.a)
        created = client.post('/api/products', json={
            'name': 'HTTP active product', 'barcode': 'HTTP-ACTIVE', 'price': '12.50',
            'stock': 3, 'cost': '4.25'})
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        product = m.Product.query.filter_by(barcode='HTTP-ACTIVE', branch_id=self.a).one()
        product_id = product.id
        location = m.accounting.location(self.a)
        layer = m.accounting.Layer.query.filter_by(location_id=location.id, product_id=product_id).one()
        self.assertEqual((product.stock, layer.quantity, layer.value_minor), (3, 3, 1275))

        increased = client.put(f'/api/products/{product_id}', json={'stock': 6, 'cost': '5.00'})
        self.assertEqual(increased.status_code, 200, increased.get_data(as_text=True))
        m.db.session.expire_all()
        product = m.db.session.get(m.Product, product_id)
        self.assertEqual(product.stock, 6)
        self.assertEqual(m.accounting.Layer.query.filter_by(location_id=location.id, product_id=product_id).count(), 2)
        self.assertEqual(m.db.session.query(m.db.func.sum(m.accounting.Layer.remaining_minor)).filter_by(
            location_id=location.id, product_id=product_id).scalar(), 2775)

        reduced = client.put(f'/api/products/{product_id}', json={'stock': 2})
        self.assertEqual(reduced.status_code, 200, reduced.get_data(as_text=True))
        m.db.session.expire_all()
        product = m.db.session.get(m.Product, product_id)
        self.assertEqual(product.stock, 2)
        self.assertEqual(m.db.session.query(m.db.func.sum(m.accounting.Layer.remaining)).filter_by(
            location_id=location.id, product_id=product_id).scalar(), 2)
        self.assertEqual(m.db.session.query(m.db.func.sum(m.accounting.Layer.remaining_minor)).filter_by(
            location_id=location.id, product_id=product_id).scalar(), 1000)
        self.assertEqual(m.accounting.Journal.query.filter_by(source_type='stock-adjustment').count(), 3)

        renamed = client.put(f'/api/products/{product_id}', json={'name': 'Renamed active product', 'price': '13.00'})
        self.assertEqual(renamed.status_code, 200, renamed.get_data(as_text=True))
        m.db.session.expire_all()
        product = m.db.session.get(m.Product, product_id)
        self.assertEqual((product.name, product.stock), ('Renamed active product', 2))
        self.assertEqual(m.db.session.query(m.db.func.sum(m.accounting.Layer.remaining_minor)).filter_by(
            location_id=location.id, product_id=product_id).scalar(), 1000)

    def test_accounting_enabled_empty_branches_deactivate_without_orphaning_locations(self):
        m = self.module
        self.activate_accounting()
        http_branch = m.Branch(name='HTTP empty shop', code='HE', is_active=True, is_default=False)
        ai_branch = m.Branch(name='AI empty shop', code='AE', is_active=True, is_default=False)
        m.db.session.add_all([http_branch, ai_branch])
        m.db.session.commit()
        http_id, ai_id = http_branch.id, ai_branch.id
        client = self.app.test_client()
        with client.session_transaction() as current:
            current.update(user_id=self.user, username='manager', role='manager', branch_id=self.a)
        response = client.delete(f'/api/branches/{http_id}')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn('deactivated', response.json['message'])
        self.assertFalse(m.db.session.get(m.Branch, http_id).is_active)
        self.assertIsNotNone(m.accounting.Location.query.filter_by(branch_id=http_id).one())

        result = self.tools.deactivate_branch(ai_id)
        self.assertTrue(result['success'], result)
        self.assertTrue(result['deactivated'])
        self.assertFalse(m.db.session.get(m.Branch, ai_id).is_active)
        self.assertIsNotNone(m.accounting.Location.query.filter_by(branch_id=ai_id).one())

    def test_http_debt_payment_replays_key_and_posts_one_collection(self):
        import uuid
        m = self.module
        client = self.app.test_client()
        with client.session_transaction() as current:
            current.update(user_id=self.user, username='manager', role='manager', branch_id=self.a)
        setup = client.post('/api/accounting/setup', json={
            'key': str(uuid.uuid4()), 'confirm': True, 'stock_costs_reviewed': True,
            'opening_balances': [], 'payables': []})
        self.assertEqual(setup.status_code, 200,
                         (setup.get_data(as_text=True), client.get('/api/accounting/opening').get_json()))
        payload = {'amount': '5.005', 'notes': 'Counter cash', 'idempotency_key': 'http-collect-1'}
        first = client.post(f'/api/debts/{self.debt_a}/payment', json=payload)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(first.json['amount_paid'], '5.01')
        self.assertEqual(first.json['remaining_balance'], 4.99)
        payment_id = first.json['payment_id']
        replay = client.post(f'/api/debts/{self.debt_a}/payment', json=payload)
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertTrue(replay.json['replayed'])
        self.assertEqual(replay.json['payment_id'], payment_id)
        conflict = client.post(f'/api/debts/{self.debt_a}/payment', json=dict(payload, amount='5.02'))
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(m.DebtPayment.query.filter_by(debt_id=self.debt_a).count(), 1)
        self.assertEqual(m.db.accounting.Journal.query.filter_by(source_type='collection').count(), 1)
        self.assertEqual(m.db.session.get(m.Debt, self.debt_a).balance, 4.99)

    def test_managers_read_update_shared_supplier_and_global_duplicate(self):
        t, m = self.tools, self.module
        self.assertEqual(t.get_supplier_list()['suppliers'][0]['supplier_id'], self.supplier)
        self.assertEqual(t.get_supplier_details(self.supplier)['name'], 'Shared supplier')
        self.assertTrue(t.update_supplier(self.supplier, phone='123', category_id=self.unique)['success'])
        self.assertEqual(m.db.session.get(m.Supplier, self.supplier).category_id, self.unique)
        self.assertIn('already exists', t.create_supplier('shared SUPPLIER')['error'])
        result = t.create_supplier('New supplier', category='Beta only')
        self.assertTrue(result['success'], result)
        self.assertEqual(m.db.session.get(m.Supplier, result['supplier_id']).branch_id, self.a)

    def test_supplier_category_ambiguity_is_explicit_and_atomic(self):
        t, m = self.tools, self.module
        self.assertEqual(len(t.get_supplier_categories()['categories']), 3)
        result = t.create_supplier('Ambiguous', category='common')
        self.assertIn('ambiguous', result['error'])
        self.assertEqual({c['category_id'] for c in result['category_choices']}, {self.ca, self.cb})
        self.assertIn('ambiguous', t.update_supplier(self.supplier, phone='BAD', category='Common')['error'])
        m.db.session.commit()
        self.assertIsNone(m.db.session.get(m.Supplier, self.supplier).phone)
        self.assertIn('do not match', t.update_supplier(self.supplier, category='Wrong', category_id=self.cb)['error'])
        self.assertTrue(t.update_supplier(self.supplier, category_id=self.cb)['success'])
        self.assertTrue(t.update_supplier(self.supplier, category='')['success'])
        self.assertIsNone(m.db.session.get(m.Supplier, self.supplier).category_id)

    def test_posted_accounting_product_deletion_is_rejected_before_cleanup(self):
        m = self.module
        self.activate_accounting()
        client = self.app.test_client()
        with client.session_transaction() as current:
            current.update(user_id=self.user, username='manager', role='manager', branch_id=self.a)
        response = client.delete(f'/api/products/{self.local}?force=1&cascade=1')
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertIn('retained financial history', response.get_json()['message'])
        self.assertIsNotNone(m.db.session.get(m.Product, self.local))

    def test_post_activation_product_deletion_preserves_pre_activation_sales(self):
        m = self.module
        product = m.Product(name='Sold out before activation', barcode='HISTORICAL',
                            stock=0, price=10, cost=2, branch_id=self.a)
        m.db.session.add(product)
        m.db.session.flush()
        sale = m.Sale(transaction_id='historical-sale', branch_id=self.a,
                      user_id=self.user, total=10, tax=0, payment_method='cash',
                      receipt_snapshot='{"items": []}')
        m.db.session.add(sale)
        m.db.session.flush()
        item = m.SaleItem(sale_id=sale.id, product_id=product.id,
                          quantity=1, price=10, tax=0)
        m.db.session.add(item)
        m.db.session.commit()
        self.assertIsNone(m.accounting.Layer.query.filter_by(product_id=product.id).first())
        self.activate_accounting()
        client = self.app.test_client()
        with client.session_transaction() as current:
            current.update(user_id=self.user, username='manager', role='manager', branch_id=self.a)
        response = client.delete(f'/api/products/{product.id}?force=1&cascade=1')
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertIn('retained financial history', response.get_json()['message'])
        self.assertIsNotNone(m.db.session.get(m.Product, product.id))
        self.assertEqual(m.db.session.get(m.SaleItem, item.id).product_id, product.id)

    def test_shared_supplier_delete_preserves_closed_po_and_linked_history(self):
        t, m = self.tools, self.module
        t.cancel_purchase_order(self.po, 'Closed')
        self.assertIn('linked records', t.delete_supplier(self.supplier)['error'])
        for model, values in (
            (m.SupplierCommunication, {'communication_type': 'email'}),
            (m.SupplierPriceAgreement, {'product_id': self.local, 'agreed_price': 2}),
        ):
            supplier = m.Supplier(name=model.__name__, branch_id=self.b)
            m.db.session.add(supplier)
            m.db.session.flush()
            m.db.session.add(model(supplier_id=supplier.id, **values))
            m.db.session.commit()
            self.assertIn('linked records', t.delete_supplier(supplier.id)['error'])
        unused = m.Supplier(name='Unused', branch_id=self.b)
        m.db.session.add(unused)
        m.db.session.commit()
        self.assertTrue(t.delete_supplier(unused.id)['success'])

    def test_local_category_rename_syncs_shared_supplier_reference_only(self):
        t, m = self.tools, self.module
        t.update_supplier(self.supplier, category_id=self.ca)
        self.assertTrue(t.update_category(self.ca, 'Renamed')['success'])
        self.assertEqual(m.db.session.get(m.Supplier, self.supplier).category, 'Renamed')
        self.assertEqual(m.db.session.get(m.Category, self.cb).name, 'Common')
        self.assertIn('error', t.delete_category(self.ca))

    def test_supplier_price_keeps_product_local_but_supplier_shared(self):
        t, m = self.tools, self.module
        m.db.session.add_all([
            m.SupplierPriceAgreement(supplier_id=self.supplier, product_id=self.local, agreed_price=1),
            m.SupplierPriceAgreement(supplier_id=self.supplier, product_id=self.foreign, agreed_price=2)])
        m.db.session.commit()
        self.assertTrue(t.get_supplier_price_for_product(self.local, self.supplier)['has_agreement'])
        self.assertFalse(t.get_supplier_price_for_product(self.foreign, self.supplier)['has_agreement'])

    def test_shared_po_catalog_create_approve_receive_cancel(self):
        t, m = self.tools, self.module
        self.assertEqual({p['product_id'] for p in t.get_purchase_order_products()['products']},
                         {self.local, self.foreign})
        self.assertEqual(t.get_purchase_orders()['orders'][0]['po_id'], self.po)
        self.assertTrue(t.approve_purchase_order(self.po)['success'])
        result = t.receive_purchase_order(self.po, idempotency_key='receive-all')
        self.assertTrue(result['success'], result)
        received = m.WarehouseInventory.query.filter_by(batch_number='B-PO').one()
        self.assertEqual((received.branch_id, received.product_id, received.quantity), (self.b, self.foreign, 5))
        self.assertEqual(m.db.session.get(m.Product, self.foreign).stock, 8)
        replay = t.receive_purchase_order(self.po, idempotency_key='receive-all')
        self.assertTrue(replay['replayed'], replay)
        self.assertEqual(m.WarehouseInventory.query.count(), 2)
        conflict = t.receive_purchase_order(self.po, [
            {'purchase_order_item_id': self.line, 'received_qty': 1}], idempotency_key='receive-all')
        self.assertIn('error', conflict)
        self.assertEqual(m.WarehouseInventory.query.count(), 2)
        result = t.create_purchase_order(self.supplier, [{'product_id': self.foreign, 'quantity': 2}])
        self.assertTrue(result['success'], result)
        self.assertTrue(t.cancel_purchase_order(result['po_id'], 'Not needed')['success'])

    def test_receiving_validates_entire_payload_and_counts_order_once(self):
        t, m = self.tools, self.module
        t.approve_purchase_order(self.po)
        result = t.receive_purchase_order(self.po, [
            {'purchase_order_item_id': self.line, 'received_qty': 2},
            {'purchase_order_item_id': self.line, 'received_qty': 4}], idempotency_key='duplicate-lines')
        self.assertIn('error', result)
        self.assertEqual(m.db.session.get(m.PurchaseOrderItem, self.line).received_qty, 0)
        self.assertEqual(m.WarehouseInventory.query.count(), 1)
        for quantity in (2, 3):
            self.assertTrue(t.receive_purchase_order(self.po, [
                {'purchase_order_item_id': self.line, 'received_qty': quantity}],
                idempotency_key=f'receive-{quantity}')['success'])
        self.assertEqual(m.db.session.get(m.Supplier, self.supplier).total_orders, 1)
        self.assertIn('error', t.receive_purchase_order(self.po, idempotency_key='receive-empty'))

    def test_warehouse_shared_transfer_defaults_to_trusted_branch(self):
        t, m = self.tools, self.module
        self.assertEqual(t.get_warehouse_inventory()['warehouse_items'][0]['product_id'], self.foreign)
        result = t.create_warehouse_transfer(self.foreign, 3, idempotency_key='transfer-default')
        self.assertTrue(result['success'], result)
        target = m.db.session.get(m.Product, result['target_product_id'])
        self.assertEqual((target.branch_id, target.barcode, target.stock), (self.a, 'PHYSICAL', 3))
        self.assertEqual(target.category_ref.branch_id, self.a)
        self.assertEqual(m.db.session.get(m.Product, self.foreign).stock, 8)
        self.assertEqual(m.WarehouseInventory.query.one().quantity, 7)
        self.assertEqual(t.get_warehouse_transfer_history()['transfers'][0]['target_branch_id'], self.a)
        replay = t.create_warehouse_transfer(self.foreign, 3, idempotency_key='transfer-default')
        self.assertTrue(replay['replayed'], replay)
        self.assertEqual((m.WarehouseInventory.query.one().quantity, target.stock), (7, 3))
        self.assertEqual(m.WarehouseTransfer.query.count(), 1)
        conflict = t.create_warehouse_transfer(self.foreign, 2, idempotency_key='transfer-default')
        self.assertIn('error', conflict)
        self.assertEqual(m.WarehouseTransfer.query.count(), 1)
        result = t.create_warehouse_transfer(self.foreign, 2, target_branch_id=self.b,
                                             idempotency_key='transfer-second')
        self.assertTrue(result['success'], result)
        self.assertEqual(m.db.session.get(m.Product, self.foreign).stock, 10)

    def test_warehouse_never_merges_unrelated_same_name_and_rolls_back_failure(self):
        t, m = self.tools, self.module
        m.db.session.get(m.Product, self.local).name = 'Other'
        m.db.session.commit()
        with patch.object(m.db.session, 'commit', side_effect=RuntimeError('injected')):
            self.assertIn('error', t.create_warehouse_transfer(self.foreign, 3,
                                                                idempotency_key='transfer-retry'))
        self.assertEqual(m.WarehouseInventory.query.one().quantity, 10)
        self.assertEqual(m.Product.query.count(), 2)
        result = t.create_warehouse_transfer(self.foreign, 3, idempotency_key='transfer-retry')
        self.assertTrue(result['success'], result)
        self.assertNotEqual(result['target_product_id'], self.local)
        self.assertEqual(m.db.session.get(m.Product, self.local).stock, 4)

    def test_transfer_rejects_catalog_barcode_drift_without_mutations(self):
        t, m = self.tools, self.module
        result = t.create_warehouse_transfer(self.foreign, 3, idempotency_key='transfer-drift')
        self.assertTrue(result['success'], result)
        target_id = result['target_product_id']
        for barcode in ('NEW', None):
            with self.subTest(destination_barcode=barcode):
                target = m.db.session.get(m.Product, target_id)
                target.barcode = barcode
                m.db.session.commit()
                result = t.create_warehouse_transfer(self.foreign, 2, idempotency_key='transfer-drift-retry')
                self.assertIn('reconciliation', result['error'])
                # A later commit must not persist any attempted FIFO debit.
                m.db.session.commit()
                self.assertEqual(m.WarehouseInventory.query.one().quantity, 7)
                self.assertEqual(m.WarehouseTransfer.query.count(), 1)
                self.assertEqual(m.db.session.get(m.Product, target_id).stock, 3)
                self.assertEqual(m.db.session.get(m.Product, target_id).barcode, barcode)
                self.assertEqual(m.db.session.get(m.Product, self.foreign).stock, 8)
                self.assertEqual(m.db.session.get(m.Product, self.foreign).barcode, 'PHYSICAL')

    def test_invalid_transfer_quantities_never_mutate(self):
        for quantity in (0, -1, True, 1.5, 'bad'):
            with self.subTest(quantity=quantity):
                self.assertIn('error', self.tools.create_warehouse_transfer(
                    self.foreign, quantity, idempotency_key='transfer-invalid'))
        self.assertEqual(self.module.WarehouseInventory.query.one().quantity, 10)

    def test_concurrent_shared_transfers_cannot_oversell(self):
        m = self.module

        def transfer(_):
            with self.app.app_context():
                tools = AITools(m.db, m.AI_MODELS)
                tools.set_context(dict(user_id=self.user, role='manager', branch_id=self.a))
                try:
                    return tools.create_warehouse_transfer(self.foreign, 7,
                                                           idempotency_key=f'concurrent-{_}')
                finally:
                    m.db.session.remove()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(transfer, range(2)))
        self.assertEqual(sum(bool(r.get('success')) for r in results), 1, results)
        m.db.session.expire_all()
        self.assertEqual(m.WarehouseInventory.query.one().quantity, 3)
        self.assertEqual(m.WarehouseTransfer.query.count(), 1)

    def test_products_customers_debts_categories_remain_local(self):
        t = self.tools
        self.assertEqual([p['product_id'] for p in t.get_inventory_status()['inventory']], [self.local])
        self.assertEqual(t.get_product_details(barcode='PHYSICAL')['error'], 'Product not found')
        self.assertIn('error', t.upsert_product('No', 1, product_id=self.foreign))
        self.assertIn('error', t.adjust_product_stock(self.foreign, 1, 'No'))
        self.assertEqual([c['name'] for c in t.get_customer_summary()['customers']], ['Local customer'])
        self.assertIn('error', t.update_customer(self.customer, name='No'))
        self.assertEqual(len(t.get_debt_summary()['debts']), 1)
        self.assertIn('error', t.write_off_debt(self.debt, 'No'))
        self.assertEqual([c['id'] for c in t.get_category_summary()['categories']], [self.ca])
        self.assertIn('error', t.update_category(self.cb, 'No'))
        self.assertIn('error', t.delete_category(self.cb))
        self.assertIn('error', t.upsert_product('Bad category', 1, category='Beta only'))

    def test_product_barcode_is_branch_unique_and_errors_leave_session_usable(self):
        t = self.tools
        result = t.upsert_product('Local physical copy', 1, barcode='PHYSICAL')
        self.assertTrue(result['success'], result)
        self.assertIn('Barcode already exists', t.upsert_product('Duplicate', 1, barcode='PHYSICAL')['error'])
        self.assertIn('error', t.upsert_product('Local', 1, barcode='PHYSICAL', product_id=self.local))
        self.assertTrue(t.upsert_product('No barcode one', 1, barcode='')['success'])
        self.assertTrue(t.upsert_product('No barcode two', 1, barcode='')['success'])

    def test_cashier_scope_still_cannot_reach_another_branch(self):
        """No per-tool role gate remains, so branch scoping is the guard."""
        with self.app.test_request_context('/'):
            session.update(user_id=self.user, role='cashier', branch_id=self.a)
            self.assertEqual(self.tools.get_product_details(product_id=self.foreign)['error'],
                             'Product not found')
            inventory = self.tools.get_inventory_status()
            self.assertEqual(inventory['branch_id'], self.a)
            self.assertEqual([p['product_id'] for p in inventory['inventory']], [self.local])


if __name__ == '__main__':
    unittest.main()