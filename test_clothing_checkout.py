"""Synthetic clothing checkout journeys; run only with python run_tests.py.

The Flask root and instance are isolated before app import. All provider HTTP is
mocked at the transport, including a fail-closed guard against real requests.
"""

import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask, session
from sqlalchemy import MetaData, create_engine, inspect, text


class ClothingCheckoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='pos-clothing-checkout-')
        root = Path(cls.temporary.name)
        shutil.copytree(Path(__file__).parent / 'templates', root / 'templates')
        original = Flask.__init__

        def isolated_init(app, *args, **kwargs):
            kwargs.update(root_path=str(root), instance_path=str(root / 'instance'))
            original(app, *args, **kwargs)

        spec = importlib.util.spec_from_file_location(
            '_clothing_checkout_test_app', Path(__file__).with_name('app.py'))
        cls.m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.m
        with patch.object(Flask, '__init__', isolated_init):
            spec.loader.exec_module(cls.m)
        cls.m.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        with cls.m.app.app_context():
            cls.m.db.session.remove()
            cls.m.db.engine.dispose()
        sys.modules.pop(cls.m.__name__, None)
        cls.temporary.cleanup()

    def setUp(self):
        m = self.m
        self.network = patch('requests.sessions.Session.request',
                             side_effect=AssertionError('Real HTTP forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        with m.app.app_context():
            m.db.drop_all()
            m.db.create_all()
            local = m.Branch(name='Shop', code='SHOP', is_default=True)
            source = m.Branch(name='Depot catalog', code='DEPOT')
            other = m.Branch(name='Other shop', code='OTHER')
            m.db.session.add_all([local, source, other])
            m.db.session.flush()
            user = m.User(username='checkout-manager', password='synthetic',
                          role='manager', branch_id=local.id)
            supplier = m.Supplier(name='Synthetic supplier', branch_id=source.id)
            m.db.session.add_all([user, supplier])
            m.db.session.commit()
            self.local, self.source, self.other = local.id, source.id, other.id
            self.user, self.supplier = user.id, supplier.id
        self.client = self.client_for(self.local)
        self.depot = self.client_for(self.source)
        created = self.depot.post('/api/clothing/styles', json={
            'name': 'Cotton Shirt', 'code': 'SHIRT', 'brand': 'Synthetic', 'season': 'Summer'})
        self.assertEqual(created.status_code, 201, created.get_json())
        self.style = created.get_json()['style']['id']
        self.variants = {}
        for color in ('Black', 'White'):
            for size in ('S', 'M', 'L'):
                response = self.depot.post(f'/api/clothing/styles/{self.style}/variants', json={
                    'size': size, 'color': color, 'barcode': f'SH-{color}-{size}',
                    'price': '100.00', 'cost': '40.00'})
                self.assertEqual(response.status_code, 201, response.get_json())
                self.variants[color, size] = response.get_json()['variant']
                taxed = self.depot.put('/api/products/' + str(response.get_json()['variant']['product_id']),
                                       json={'tax_rate': 10})
                self.assertEqual(taxed.status_code, 200, taxed.get_json())
        with m.app.app_context():
            po = m.PurchaseOrder(po_number='PO-SHIRTS', supplier_id=self.supplier,
                status='approved', total_amount=2400, created_by=self.user, branch_id=self.local)
            m.db.session.add(po)
            m.db.session.flush()
            lines = []
            for variant in self.variants.values():
                line = m.PurchaseOrderItem(purchase_order_id=po.id,
                    product_id=variant['product_id'], ordered_qty=10, received_qty=0, unit_cost=40)
                m.db.session.add(line)
                lines.append(line)
            m.db.session.flush()
            self.po = po.id
            receive_items = [dict(purchase_order_item_id=line.id, received_qty=10) for line in lines]
            self.receive_items = receive_items
            m.db.session.commit()
        received = self.client.post(f'/api/purchase_orders/{self.po}/receive', json={
            'items': receive_items, 'idempotency_key': 'receive-shirts'})
        self.assertEqual(received.status_code, 200, received.get_json())
        for variant in self.variants.values():
            moved = self.transfer(variant['product_id'], 6, self.local)
            self.assertEqual(moved.status_code, 200, moved.get_json())
            variant['local_id'] = moved.get_json()['target_product_id']

    def client_for(self, branch):
        client = self.m.app.test_client()
        with client.session_transaction() as state:
            state.update(user_id=self.user, username='checkout-manager', role='manager', branch_id=branch)
        return client

    def transfer(self, product, quantity, branch, **changes):
        payload = dict(product_id=product, quantity=quantity, target_branch_id=branch)
        payload.update(changes)
        return self.client.post('/api/warehouse/transfer', json=payload)

    def checkout(self, variant=None, **changes):
        variant = variant or self.variants['Black', 'M']
        result = dict(items=[dict(product_id=variant['local_id'], quantity=1, price=100)],
                      transaction_id='checkout-shirt', payment_method='cash', cash_received=200)
        result.update(changes)
        return result

    def stock(self, product):
        with self.m.app.app_context():
            return self.m.db.session.get(self.m.Product, product).stock

    def test_six_variants_receive_to_warehouse_then_scan_exact_branch_variant(self):
        m = self.m
        retried = self.client.post(f'/api/purchase_orders/{self.po}/receive', json={
            'items': self.receive_items, 'idempotency_key': 'receive-shirts'})
        self.assertEqual(retried.status_code, 200, retried.get_json())
        self.assertTrue(retried.get_json()['replayed'])
        listed = self.client.get(f'/api/clothing/styles/{self.style}/variants').get_json()['variants']
        self.assertEqual({(row['color'], row['size']) for row in listed},
                         {(color, size) for color in ('Black', 'White') for size in ('S', 'M', 'L')})
        for (color, size), variant in self.variants.items():
            with self.subTest(color=color, size=size):
                result = self.client.get('/api/products/search', query_string={'q': variant['barcode']})
                self.assertEqual(result.status_code, 200)
                rows = result.get_json()
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]['id'], rows[0]['size'], rows[0]['color'], rows[0]['stock']),
                                 (variant['local_id'], size, color, 6))
                self.assertEqual(rows[0]['catalog_id'], variant['catalog_id'])
                self.assertEqual(self.stock(variant['product_id']), 0)
                with m.app.app_context():
                    self.assertEqual(sum(row.quantity for row in m.WarehouseInventory.query.filter_by(
                        product_id=variant['product_id'])), 4)
        with m.app.app_context():
            self.assertEqual(m.db.session.get(m.PurchaseOrder, self.po).status, 'received')
            self.assertEqual(m.db.session.get(m.Supplier, self.supplier).total_orders, 1)

    def test_promotion_tax_and_receipt_and_sale_details_keep_original_variant_snapshot(self):
        m = self.m
        chosen = self.variants['Black', 'M']
        with m.app.app_context():
            m.db.session.add(m.Promotion(product_id=chosen['local_id'], discount_type='percent',
                discount_value=20, start_date=datetime.now() - timedelta(days=2),
                end_date=datetime.now() + timedelta(days=2)))
            m.db.session.commit()
        invalid = self.checkout(items=[dict(product_id=chosen['local_id'], quantity=2, price=70)])
        self.assertEqual(self.client.post('/api/sales', json=invalid).status_code, 400)
        payload = self.checkout(items=[dict(product_id=chosen['local_id'], quantity=2, price=80, tax_rate=0)])
        response = self.client.post('/api/sales', json=payload)
        self.assertEqual(response.status_code, 201, response.get_json())
        detail = self.client.get('/api/sales/checkout-shirt').get_json()
        self.assertEqual((detail['total'], detail['tax'], detail['refund_amount']), (176, 16, 24))
        line = detail['items'][0]
        self.assertEqual((line['name'], line['display_name'], line['size'], line['color']),
                         ('Cotton Shirt — Black / M', 'Cotton Shirt — Black / M', 'M', 'Black'))
        self.assertEqual((line['quantity'], line['price'], line['tax'], line['tax_rate']), (2, 80, 16, 10))
        with m.app.app_context():
            sale = m.Sale.query.filter_by(transaction_id='checkout-shirt').one()
            frozen_receipt = sale.receipt_snapshot
            frozen_line = m.SaleItem.query.filter_by(sale_id=sale.id).one().display_snapshot
            self.assertEqual(json.loads(frozen_line)['barcode'], 'SH-Black-M')
            receipt_item = json.loads(frozen_receipt)['items'][0]
            self.assertEqual((receipt_item['size'], receipt_item['color'], receipt_item['quantity']),
                             ('M', 'Black', 2))
            style = m.db.session.get(m.ClothingStyle, self.style)
            style.name, style.brand, style.season = 'Renamed Shirt', 'Changed', 'Winter'
            variant = m.db.session.get(m.ClothingVariant, chosen['variant_id'])
            variant.size, variant.color = 'XL', 'Red'
            product = m.db.session.get(m.Product, chosen['local_id'])
            product.name, product.tax_rate, product.barcode = 'Changed product', 99, 'CHANGED'
            m.db.session.commit()
        after = self.client.get('/api/sales/checkout-shirt').get_json()
        self.assertEqual(after['items'], detail['items'])
        printed = self.client.get('/api/sales/checkout-shirt/print')
        self.assertEqual(printed.status_code, 200)
        body = printed.get_data(as_text=True)
        self.assertIn('Cotton Shirt', body)
        self.assertIn('Black', body)
        self.assertIn('176.00', body)
        self.assertNotIn('Renamed Shirt', body)
        with m.app.app_context():
            self.assertEqual(m.Sale.query.filter_by(transaction_id='checkout-shirt').one().receipt_snapshot, frozen_receipt)
            self.assertEqual(m.SaleItem.query.one().display_snapshot, frozen_line)
        self.assertEqual(self.stock(chosen['local_id']), 4)
        self.assertEqual(self.stock(self.variants['White', 'M']['local_id']), 6)

    def test_branch_stock_isolation_and_sale_visibility(self):
        variant = self.variants['Black', 'M']
        moved = self.transfer(variant['product_id'], 2, self.other)
        self.assertEqual(moved.status_code, 200, moved.get_json())
        other_id = moved.get_json()['target_product_id']
        other = self.client_for(self.other)
        scan = other.get('/api/products/search?q=SH-Black-M').get_json()
        self.assertEqual([(row['id'], row['stock']) for row in scan], [(other_id, 2)])
        denied = other.post('/api/sales', json=self.checkout())
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(self.client.post('/api/sales', json=self.checkout()).status_code, 201)
        self.assertEqual(other.get('/api/sales/checkout-shirt').status_code, 404)
        self.assertEqual(other.get('/api/sales/checkout-shirt/print').status_code, 404)
        self.assertEqual(self.stock(variant['local_id']), 5)
        self.assertEqual(self.stock(other_id), 2)

    def test_offline_duplicate_retries_do_not_sell_or_snapshot_twice(self):
        payload = self.checkout(transaction_id='offline-clothing-retry')
        first = self.client.post('/api/sales', json=payload)
        self.assertEqual(first.status_code, 201, first.get_json())
        before = self.client.get('/api/sales/offline-clothing-retry').get_json()
        for _ in range(3):
            response = self.client.post('/api/sales', json=payload)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertTrue(response.get_json()['duplicate'])
        self.assertEqual(self.client.get('/api/sales/offline-clothing-retry').get_json(), before)
        with self.m.app.app_context():
            self.assertEqual(self.m.Sale.query.count(), 1)
            self.assertEqual(self.m.SaleItem.query.count(), 1)
        self.assertEqual(self.stock(self.variants['Black', 'M']['local_id']), 5)

    def test_archived_variant_and_style_reject_checkout_without_mutation(self):
        for scope in ('variant', 'style'):
            with self.subTest(scope=scope):
                if scope == 'variant':
                    endpoint = f"/api/clothing/variants/{self.variants['Black', 'M']['variant_id']}"
                else:
                    endpoint = f'/api/clothing/styles/{self.style}'
                archived = self.client.put(endpoint, json={'archived': True})
                self.assertEqual(archived.status_code, 200, archived.get_json())
                rejected = self.client.post('/api/sales', json=self.checkout())
                self.assertEqual(rejected.status_code, 409, rejected.get_json())
                self.assertIn('archived', rejected.get_json()['message'].lower())
                self.assertEqual(self.stock(self.variants['Black', 'M']['local_id']), 6)
                self.assertEqual(self.client.put(endpoint, json={'archived': False}).status_code, 200)
        with self.m.app.app_context():
            self.assertEqual(self.m.Sale.query.count(), 0)

    def test_ordinary_product_sale_and_legacy_return_payload_remain_compatible(self):
        m = self.m
        with m.app.app_context():
            product = m.Product(name='Ordinary notebook', barcode='NOTEBOOK', price=50,
                                stock=3, tax_rate=10, branch_id=self.local)
            m.db.session.add(product)
            m.db.session.commit()
            product_id = product.id
        response = self.client.post('/api/sales', json=self.checkout(
            items=[dict(product_id=product_id, quantity=2, price=50)], transaction_id='ordinary-sale'))
        self.assertEqual(response.status_code, 201, response.get_json())
        detail = self.client.get('/api/sales/ordinary-sale').get_json()
        self.assertEqual((detail['total'], detail['tax']), (110, 10))
        self.assertEqual(detail['items'][0]['name'], 'Ordinary notebook')
        self.assertNotIn('variant_id', detail['items'][0])
        returned = self.client.post('/api/returns_exchanges', json={
            'original_transaction_id': 'ordinary-sale', 'return_items': [
                {'sale_item_id': detail['items'][0]['sale_item_id'], 'quantity': 1}]})
        self.assertEqual(returned.status_code, 201, returned.get_json())
        self.assertEqual(returned.get_json()['return_total'], '55.00')
        self.assertEqual(self.stock(product_id), 2)
        after = self.client.get('/api/sales/ordinary-sale').get_json()
        self.assertEqual((after['total'], after['tax']), (110, 10))
        self.assertEqual(after['items'][0]['available_return_quantity'], 1)

    def test_attached_barcode_less_product_remains_editable(self):
        m = self.m
        with m.app.app_context():
            product = m.Product(branch_id=self.local, catalog_id='bare-catalog',
                                barcode=None, name='Barcode-less shirt', price=30,
                                cost=10, stock=4)
            m.db.session.add(product)
            m.db.session.commit()
            product_id = product.id
        attached = self.client.post(f'/api/clothing/styles/{self.style}/attach',
            json=dict(product_id=product_id, size='XL', color='Green'))
        self.assertEqual(attached.status_code, 201, attached.get_json())
        updated = self.client.put(f'/api/products/{product_id}',
            json=dict(barcode='', name='Barcode-less shirt updated', cost=11))
        self.assertEqual(updated.status_code, 200, updated.get_json())
        with m.app.app_context():
            product = m.db.session.get(m.Product, product_id)
            self.assertEqual((product.name, product.cost, product.stock),
                             ('Barcode-less shirt updated', 11, 4))
        # Assigning a barcode would change the variant's physical identity.
        blocked = self.client.put(f'/api/products/{product_id}', json=dict(barcode='NEW-BARCODE'))
        self.assertEqual(blocked.status_code, 409)

    def test_clothing_barcode_conflict_rejects_transfer_but_general_barcode_matching_survives(self):
        m = self.m
        chosen = self.variants['Black', 'M']
        with m.app.app_context():
            collision = m.Product(name='Different identity', barcode=chosen['barcode'], price=100,
                stock=3, branch_id=self.other)
            ordinary_source = m.Product(name='General source', barcode='GENERAL', price=10,
                stock=0, branch_id=self.source)
            ordinary_target = m.Product(name='General destination', barcode='GENERAL', price=10,
                stock=7, branch_id=self.other)
            m.db.session.add_all([collision, ordinary_source, ordinary_target])
            m.db.session.flush()
            m.db.session.add(m.WarehouseInventory(product_id=ordinary_source.id, quantity=4,
                unit_cost=5, branch_id=self.source))
            m.db.session.commit()
            collision_id, source_id, target_id = collision.id, ordinary_source.id, ordinary_target.id
        conflict = self.transfer(chosen['product_id'], 1, self.other)
        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(self.stock(collision_id), 3)
        with m.app.app_context():
            self.assertEqual(sum(row.quantity for row in m.WarehouseInventory.query.filter_by(
                product_id=chosen['product_id'])), 4)
            target = m.db.session.get(m.Product, collision_id)
            target.catalog_id, target.barcode = chosen['catalog_id'], 'WRONG-PHYSICAL-BARCODE'
            m.db.session.commit()
        mismatched_copy = self.transfer(chosen['product_id'], 1, self.other)
        self.assertEqual(mismatched_copy.status_code, 409, mismatched_copy.get_json())
        self.assertEqual(self.stock(collision_id), 3)
        general = self.transfer(source_id, 2, self.other)
        self.assertEqual(general.status_code, 200, general.get_json())
        self.assertEqual(general.get_json()['target_product_id'], target_id)
        self.assertEqual(self.stock(target_id), 9)

    def test_mmqr_same_amount_variant_change_cannot_spend_saved_payment(self):
        from test_myanmyanpay_client import response as provider_response

        m = self.m
        ledger, calls = {}, []
        root = '/api/payments/myanmyanpay/intents'
        headers = {'X-POS-Payment-Request': '1'}

        def transport(url, *, data, headers, **kwargs):
            operation = url.rsplit('/', 1)[-1]
            calls.append(operation)
            payload = json.loads(data)
            expected = hmac.new(b'sk_test_checkout',
                headers['X-Mmpay-Nonce'].encode() + b'.' + data, hashlib.sha256).hexdigest()
            self.assertEqual(headers['X-Mmpay-Signature'], expected)
            self.assertEqual(headers['Authorization'], 'Bearer pk_test_checkout')
            if operation == 'sandbox-handshake':
                return provider_response({'token': 'synthetic-checkout-token'})
            self.assertEqual(headers['X-Mmpay-Btoken'], 'synthetic-checkout-token')
            if operation == 'sandbox-create':
                self.assertEqual(payload['amount'], 110)
                record = dict(orderId=payload['orderId'], amount=110, currency='MMK',
                    status='PENDING', vendorQrRefId='qr-' + payload['orderId'], qr='000201TESTQR')
                ledger[payload['orderId']] = record
                return provider_response(record, 201)
            self.assertEqual(operation, 'sandbox-get')
            return provider_response(ledger[payload['orderId']])

        env = dict(MMPAY_ENABLED='true', MMPAY_APP_ID='synthetic-app',
            MMPAY_PUBLISHABLE_KEY='pk_test_checkout', MMPAY_SECRET_KEY='sk_test_checkout',
            MMPAY_API_BASE_URL='https://api.myanmyanpay.com',
            MMPAY_CALLBACK_URL='https://pos.example/webhooks/myanmyanpay')
        with patch.dict(os.environ, env), patch.object(m, 'get_currency_code', return_value='MMK'), \
                patch('myanmyanpay.requests.post', side_effect=transport):
            payload = self.checkout(payment_method='mobile_payment', transaction_id='mmqr-shirt')
            payload.pop('cash_received')
            created = self.client.post(root, json=payload, headers=headers)
            self.assertEqual(created.status_code, 201, created.get_json())
            payment = created.get_json()['payment']
            endpoint = root + '/' + payment['id']
            changed = dict(payload, items=[dict(product_id=self.variants['White', 'M']['local_id'],
                                              quantity=1, price=100)])
            rejected = self.client.post(root, json=changed, headers=headers)
            self.assertEqual(rejected.status_code, 409, rejected.get_json())
            self.assertEqual(self.client.post(endpoint + '/complete', headers=headers).status_code, 409)
            self.assertEqual(self.stock(self.variants['Black', 'M']['local_id']), 6)
            ledger[payment['order_id']].update(status='SUCCESS', transactionRefId='synthetic-paid')
            refreshed = self.client.post(endpoint + '/refresh', headers=headers)
            self.assertEqual(refreshed.status_code, 200, refreshed.get_json())
            self.assertEqual(refreshed.get_json()['payment']['status'], 'SUCCESS')
            # Agreed internal seam: even a caller with a verified intent cannot
            # swap equal-priced variants after payment verification.
            with m.app.test_request_context('/api/sales'):
                session.update(user_id=self.user, role='manager', branch_id=self.local)
                intent = m.db.session.get(m.PaymentIntent, payment['id'])
                tampered = json.loads(intent.checkout_json)
                tampered['items'][0]['product_id'] = self.variants['White', 'M']['local_id']
                result, status = m._create_sale_transaction(tampered, verified_payment_id=payment['id'])
                self.assertEqual(status, 409, result.get_json())
                m.db.session.rollback()
            completed = self.client.post(endpoint + '/complete', json=changed, headers=headers)
            self.assertEqual(completed.status_code, 201, completed.get_json())
            self.assertEqual(self.client.post(endpoint + '/complete', headers=headers).status_code, 200)
            self.assertEqual(calls, ['sandbox-handshake', 'sandbox-create', 'sandbox-handshake', 'sandbox-get'])
        detail = self.client.get('/api/sales/mmqr-shirt').get_json()
        self.assertEqual(detail['total'], 110)
        self.assertEqual((detail['items'][0]['color'], detail['items'][0]['size']), ('Black', 'M'))
        self.assertEqual(self.stock(self.variants['Black', 'M']['local_id']), 5)
        self.assertEqual(self.stock(self.variants['White', 'M']['local_id']), 6)
        with m.app.app_context():
            self.assertEqual(m.Sale.query.count(), 1)
            self.assertEqual(m.PaymentIntent.query.count(), 1)

    def test_sale_display_migration_preserves_full_synthetic_legacy_records(self):
        """Full model-shaped legacy schema, not a historical startup/git fixture.

        All existing Product/Sale fields and return tables are retained; only
        clothing tables and SaleItem.display_snapshot are absent. This tests the
        additive helper, not every previous deployment's startup migrations.
        """
        from clothing_integration import migrate_sale_display

        m = self.m
        legacy = MetaData()
        for table in m.db.metadata.sorted_tables:
            if not table.name.startswith('clothing_'):
                table.to_metadata(legacy)
        legacy.tables['sale_item']._columns.remove(legacy.tables['sale_item'].c.display_snapshot)
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        legacy.create_all(engine)
        with engine.begin() as connection:
            connection.execute(legacy.tables['product'].insert(), dict(id=41, name='Legacy ordinary',
                barcode='LEGACY-41', catalog_id='legacy-catalog', stock=17, price=100, tax_rate=10))
            connection.execute(legacy.tables['sale'].insert(), dict(id=73, transaction_id='legacy-sale',
                total=220, tax=20, payment_method='cash', cash_received=250))
            connection.execute(legacy.tables['sale_item'].insert(), dict(id=89, sale_id=73,
                product_id=41, quantity=2, price=100, tax=20))
            connection.execute(legacy.tables['return_exchange'].insert(), dict(id=97,
                workflow_id='legacy-return', mode='return', original_sale_id=73,
                return_total=110, refund_amount=110))
            connection.execute(legacy.tables['return_exchange_item'].insert(), dict(id=101,
                return_exchange_id=97, original_sale_item_id=89, product_id=41,
                movement='return', quantity=1, unit_price=100, tax_rate=10, line_total=100, line_tax=10))
            before = {name: connection.execute(text(f'SELECT * FROM {name}')).all()
                      for name in ('product', 'sale', 'sale_item', 'return_exchange', 'return_exchange_item')}
        adapter = SimpleNamespace(engine=engine, session=SimpleNamespace(rollback=lambda: None))
        migrate_sale_display(adapter)
        migrate_sale_display(adapter)
        with engine.connect() as connection:
            for name in ('product', 'sale', 'return_exchange', 'return_exchange_item'):
                self.assertEqual(connection.execute(text(f'SELECT * FROM {name}')).all(), before[name])
            self.assertEqual(connection.execute(text(
                'SELECT id, sale_id, product_id, quantity, price, tax FROM sale_item')).all(), before['sale_item'])
            self.assertIsNone(connection.execute(text('SELECT display_snapshot FROM sale_item')).scalar())
        columns = {column['name'] for column in inspect(engine).get_columns('sale_item')}
        self.assertIn('display_snapshot', columns)


if __name__ == '__main__':
    unittest.main()