"""Clothing views over existing stock, promotions and transaction ledgers."""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError


def register_clothing_analytics(app, db, pos):
    manager = pos['manager_or_boss_required']
    Product, Variant, Style = (pos[name] for name in ('Product', 'ClothingVariant', 'ClothingStyle'))
    Promotion = pos['Promotion']

    @app.post('/api/clothing/styles/<int:style_id>/promotions')
    @manager
    def clothing_style_promotions(style_id):
        """Expand targeting only; use the existing per-product price rules."""
        try:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise ValueError('A JSON object is required')
            branch = pos['get_current_branch_id']()
            if not branch:
                raise ValueError('Select an active branch')
            if data.get('branch_id') not in (None, branch, str(branch)):
                raise ValueError('Use the active branch')
            kind = data.get('discount_type')
            value = Decimal(str(data.get('discount_value')))
            if kind not in ('percent', 'fixed') or not value.is_finite() or value < 0 or (kind == 'percent' and value > 100):
                raise ValueError('Invalid promotion type or amount')
            zone = pos['pytz'].timezone('Asia/Yangon')
            dates = []
            for field in ('start_date', 'end_date'):
                date = datetime.fromisoformat(str(data.get(field, '')).replace('Z', '+00:00'))
                date = date.astimezone(zone) if date.tzinfo else zone.localize(date)
                dates.append(date.replace(tzinfo=None))
            start, end = dates
            if start >= end:
                raise ValueError('End date must be after start date')
            db.session.rollback()
            if db.engine.dialect.name == 'sqlite':
                db.session.execute(text('BEGIN IMMEDIATE'))
            elif db.engine.dialect.name == 'postgresql':
                db.session.execute(text('LOCK TABLE clothing_variant IN EXCLUSIVE MODE'))
            style = db.session.get(Style, style_id)
            if not style or style.archived:
                raise ValueError('Style not found or archived')
            products = Product.query.join(Variant, Product.catalog_id == Variant.catalog_id).filter(
                Product.branch_id == branch, Variant.style_id == style_id, Variant.archived.is_(False)).all()
            selected = data.get('product_ids')
            if selected is not None:
                if not isinstance(selected, list) or not selected or any(type(i) is not int for i in selected) or len(set(selected)) != len(selected):
                    raise ValueError('product_ids must be distinct integer product IDs')
                if not set(selected).issubset({p.id for p in products}):
                    raise ValueError('Selected variants must be active in this style and branch')
                products = [p for p in products if p.id in selected]
            if not products:
                raise ValueError('No active variants in this branch')
            created = []
            for product in products:
                existing = Promotion.query.filter_by(product_id=product.id, discount_type=kind,
                    discount_value=float(value), start_date=start, end_date=end).first()
                if not existing:
                    existing = Promotion(product_id=product.id, discount_type=kind,
                        discount_value=float(value), start_date=start, end_date=end)
                    db.session.add(existing)
                    db.session.flush()
                created.append(existing.id)
            db.session.commit()
            return jsonify(success=True, promotion_ids=created), 201
        except (ValueError, TypeError, InvalidOperation) as error:
            db.session.rollback()
            return jsonify(success=False, message=str(error)), 400
        except (OperationalError, IntegrityError):
            db.session.rollback()
            return jsonify(success=False, message='Catalog is busy. Retry the same request.'), 409

    @app.get('/api/reports/clothing')
    @manager
    def clothing_report():
        """Current stock and snapshotted line turnover, never guessed stock ages.

        Existing financial totals remain independent: adjustment Sale.total is a
        settlement, whereas replacement line totals describe full merchandise.
        """
        if not pos['get_current_branch_id']():
            return jsonify(success=False, message='Select an active branch'), 403
        scope, branch = pos['resolve_report_scope']()
        try:
            sales_query = pos['sales_report_query'](branch)
        except ValueError as error:
            return jsonify(success=False, message=str(error)), 400
        stock_query = Product.query.join(Variant, Product.catalog_id == Variant.catalog_id)
        if branch:
            stock_query = stock_query.filter(Product.branch_id == branch)
        stock = []
        for product in stock_query.order_by(Product.branch_id, Product.id).all():
            stock.append(dict(product_id=product.id, branch_id=product.branch_id,
                stock=product.stock or 0, barcode=product.barcode, **pos['product_clothing_display'](product)))
        SaleItem, Sale = pos['SaleItem'], pos['Sale']
        sales = sales_query.all()
        groups = {}
        def add(snapshot, branch_id, quantity, subtotal, tax, movement):
            # Null snapshots are deliberately not classified from today's catalog.
            key = (branch_id, snapshot.get('style_id'), snapshot.get('variant_id'), snapshot.get('size'), snapshot.get('color'))
            row = groups.setdefault(key, dict(branch_id=branch_id, style_id=key[1], variant_id=key[2],
                size=key[3], color=key[4], style_name=snapshot.get('style_name'),
                sold_quantity=0, returned_quantity=0, net_subtotal=Decimal('0'), net_tax=Decimal('0')))
            row['returned_quantity' if movement == 'return' else 'sold_quantity'] += quantity
            sign = -1 if movement == 'return' else 1
            row['net_subtotal'] += sign * subtotal
            row['net_tax'] += sign * tax
        sale_map = {sale.id: sale for sale in sales}
        for item in SaleItem.query.filter(SaleItem.sale_id.in_(list(sale_map))).all() if sale_map else []:
            snapshot = pos['sale_item_display'](item)
            add(snapshot, sale_map[item.sale_id].branch_id, item.quantity,
                pos['safe_to_decimal'](item.price) * item.quantity, pos['safe_to_decimal'](item.tax), 'sale')
        Return, ReturnItem = pos['ReturnExchange'], pos['ReturnExchangeItem']
        credits = ReturnItem.query.join(Return).join(Sale, Return.original_sale_id == Sale.id).filter(
            ReturnItem.movement == 'return')
        if branch:
            credits = credits.filter(Sale.branch_id == branch)
        # Match the original-sale cohort; do not pretend this is cash-basis reporting.
        credits = credits.filter(Return.original_sale_id.in_(list(sale_map)))
        for item in credits.all() if sale_map else []:
            original = db.session.get(SaleItem, item.original_sale_item_id)
            snapshot = pos['sale_item_display'](original) if original else {}
            add(snapshot, sale_map[item.return_exchange.original_sale_id].branch_id, item.quantity,
                pos['safe_to_decimal'](item.line_total), pos['safe_to_decimal'](item.line_tax), 'return')
        rows = []
        for row in groups.values():
            row['net_quantity'] = row['sold_quantity'] - row['returned_quantity']
            row['net_total'] = pos['money_float'](row['net_subtotal'] + row['net_tax'])
            row['net_subtotal'] = pos['money_float'](row['net_subtotal'])
            row['net_tax'] = pos['money_float'](row['net_tax'])
            rows.append(row)
        return jsonify(success=True, scope=scope, stock=stock, sales=rows,
            financial_totals=dict(sale_total=pos['money_float'](sum((pos['safe_to_decimal'](s.total) for s in sales), Decimal('0'))),
                                  sale_tax=pos['money_float'](sum((pos['safe_to_decimal'](s.tax) for s in sales), Decimal('0')))),
            basis='Original-sale cohort line turnover less returns; settlement totals are separate. Legacy lines remain unclassified.',
            stock_age_available=False)