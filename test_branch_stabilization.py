"""Functional branch regressions; every database/upload lives in a disposable root.

Run only this module: python -m unittest test_branch_stabilization -v
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import unittest
from unittest.mock import patch

from flask import Flask


class BranchStabilizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='pos-branch-tests-')
        cls.root = Path(cls.temporary.name)
        (cls.root / 'instance').mkdir()
        shutil.copytree(Path(__file__).parent / 'templates', cls.root / 'templates')
        original_init = Flask.__init__

        def isolated_init(app, *args, **kwargs):
            kwargs.update(instance_path=str(cls.root / 'instance'), root_path=str(cls.root))
            original_init(app, *args, **kwargs)

        name = '_isolated_branch_stabilization_app'
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
        cls.temporary.cleanup()

    def setUp(self):
        m = self.module
        with self.app.app_context():
            m.db.drop_all()
            m.db.create_all()
            a = m.Branch(name='Alpha', code='A', is_active=True, is_default=True)
            b = m.Branch(name='Beta', code='B', is_active=True)
            m.db.session.add_all([a, b])
            m.db.session.flush()
            user = m.User(username='manager', password=m.generate_password_hash('password'), role='manager', branch_id=a.id)
            cashier = m.User(username='cashier', password=m.generate_password_hash('password'), role='cashier', branch_id=b.id)
            product = m.Product(name='Same name', barcode='PHYSICAL-123', price=10, cost=3,
                                stock=5, branch_id=a.id)
            unrelated = m.Product(name='Same name', barcode='OTHER', price=25, stock=7, branch_id=b.id)
            m.db.session.add_all([user, cashier, product, unrelated])
            m.db.session.flush()
            m.db.session.add(m.WarehouseInventory(product_id=product.id, quantity=10, branch_id=a.id, batch_number='BATCH'))
            m.db.session.commit()
            self.a, self.b, self.user, self.cashier, self.product, self.unrelated = (
                a.id, b.id, user.id, cashier.id, product.id, unrelated.id)

    def client(self, branch=None, role='manager'):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=self.cashier if role == 'cashier' else self.user,
                           username=role, role=role, branch_id=branch or self.a)
        return client

    def transfer(self, client=None, **changes):
        payload = dict(product_id=self.product, quantity=3, target_branch_id=self.b)
        payload.update(changes)
        return (client or self.client()).post('/api/warehouse/transfer', json=payload)

    def test_transfer_preserves_scan_and_never_matches_name(self):
        result = self.transfer()
        self.assertEqual(result.status_code, 200, result.get_json())
        target = result.get_json()['target_product_id']
        self.assertNotEqual(target, self.unrelated)
        m = self.module
        with self.app.app_context():
            self.assertEqual(m.db.session.get(m.Product, target).barcode, 'PHYSICAL-123')
            self.assertEqual(m.db.session.get(m.Product, self.unrelated).stock, 7)
            self.assertEqual(m.WarehouseInventory.query.one().quantity, 7)
        rows = self.client(self.b).get('/api/products?q=PHYSICAL-123').get_json()
        self.assertEqual([row['id'] for row in rows], [target])

    def test_barcode_less_transfers_use_stable_identity(self):
        m = self.module
        with self.app.app_context():
            m.db.session.get(m.Product, self.product).barcode = None
            m.db.session.commit()
        first = self.transfer().get_json()
        second = self.transfer().get_json()
        self.assertEqual(first['target_product_id'], second['target_product_id'])
        self.assertEqual(second['new_main_stock'], 6)
        self.assertNotEqual(first['target_product_id'], self.unrelated)

    def test_quantity_and_identifier_validation_is_strict(self):
        for value in (None, '', 'bad', 0, -1, 1.5, '1.5', True, [], {}, 2147483648):
            with self.subTest(quantity=value):
                self.assertEqual(self.transfer(quantity=value).status_code, 400)
        self.assertEqual(self.transfer(product_id='bad').status_code, 400)
        self.assertEqual(self.transfer(target_branch_id=True).status_code, 400)
        self.assertEqual(self.client().post('/api/warehouse/transfer', json=[]).status_code, 400)

    def test_insufficient_batch_and_inactive_branch_leave_no_writes(self):
        self.assertEqual(self.transfer(quantity=11).status_code, 400)
        self.assertEqual(self.transfer(batch_number='missing').status_code, 400)
        m = self.module
        with self.app.app_context():
            m.db.session.get(m.Branch, self.b).is_active = False
            m.db.session.commit()
        self.assertEqual(self.transfer().status_code, 404)
        with self.app.app_context():
            self.assertEqual(m.WarehouseInventory.query.one().quantity, 10)
            self.assertEqual(m.WarehouseTransfer.query.count(), 0)

    def test_idempotent_retry_and_conflicting_key(self):
        first = self.transfer(idempotency_key='retry')
        second = self.transfer(idempotency_key='retry')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()['replayed'])
        self.assertEqual(first.get_json()['transfer_id'], second.get_json()['transfer_id'])
        self.assertEqual(self.transfer(idempotency_key='retry', quantity=4).status_code, 409)
        with self.app.app_context():
            self.assertEqual(self.module.WarehouseTransfer.query.count(), 1)
            self.assertEqual(self.module.WarehouseInventory.query.one().quantity, 7)

    def test_catalog_barcode_drift_rejects_before_inventory_mutations(self):
        m = self.module
        target = self.transfer().get_json()['target_product_id']
        response = self.client(self.b).put(f'/api/products/{target}', json={'barcode': 'EDITED'})
        self.assertEqual(response.status_code, 200)
        for barcode in ('EDITED', None):
            with self.subTest(barcode=barcode):
                with self.app.app_context():
                    m.db.session.get(m.Product, target).barcode = barcode
                    m.db.session.commit()
                result = self.transfer(idempotency_key='blocked-drift')
                self.assertEqual(result.status_code, 409, result.get_json())
                with self.app.app_context():
                    self.assertEqual(m.WarehouseInventory.query.one().quantity, 7)
                    self.assertEqual(m.db.session.get(m.Product, target).stock, 3)
                    self.assertEqual(m.WarehouseTransfer.query.count(), 1)
                    self.assertIsNone(m.db.session.get(m.TransferRequest, 'blocked-drift'))

    def test_idempotent_retry_survives_source_purge_and_identifier_reuse(self):
        m = self.module
        first = self.transfer(quantity=10, idempotency_key='permanent-retry')
        self.assertEqual(first.status_code, 200, first.get_json())
        result = first.get_json()
        deleted = self.client().delete(f'/api/products/{self.product}?force=1&cascade=1')
        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        with self.app.app_context():
            self.assertEqual(m.WarehouseTransfer.query.count(), 0)
            self.assertIsNotNone(m.db.session.get(m.TransferRequest, 'permanent-retry'))
        # A replay must succeed even before the deleted source is recreated.
        replay = self.transfer(quantity=10, idempotency_key='permanent-retry')
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()['replayed'])
        with self.app.app_context():
            recreated = m.Product(id=self.product, name='Reused ID', barcode='REUSED',
                                  stock=0, price=1, branch_id=self.a)
            m.db.session.add(recreated)
            m.db.session.add(m.WarehouseInventory(product_id=self.product, quantity=50, branch_id=self.a))
            m.db.session.commit()
        replay = self.transfer(quantity=10, idempotency_key='permanent-retry')
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()['transfer_id'], result['transfer_id'])
        self.assertEqual(replay.get_json()['new_main_stock'], result['new_main_stock'])
        self.assertEqual(self.transfer(quantity=9, idempotency_key='permanent-retry').status_code, 409)
        with self.app.app_context():
            self.assertEqual(m.WarehouseInventory.query.one().quantity, 50)
            self.assertEqual(m.db.session.get(m.Product, result['target_product_id']).stock, 10)
            self.assertEqual(m.WarehouseTransfer.query.count(), 0)

    def test_concurrent_transfers_never_oversell_or_lose_credits(self):
        clients = [self.client(), self.client()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda c: self.transfer(c, quantity=7).status_code, clients))
        self.assertEqual(sorted(results), [200, 400])
        m = self.module
        with self.app.app_context():
            self.assertEqual(m.WarehouseInventory.query.one().quantity, 3)
            self.assertEqual(m.WarehouseTransfer.query.count(), 1)
            self.assertEqual(m.Product.query.filter_by(branch_id=self.b, barcode='PHYSICAL-123').one().stock, 7)

    def test_concurrent_idempotent_requests_credit_once(self):
        clients = [self.client(), self.client()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda c: self.transfer(c, idempotency_key='concurrent').status_code, clients))
        self.assertEqual(results, [200, 200])
        with self.app.app_context():
            self.assertEqual(self.module.WarehouseInventory.query.one().quantity, 7)
            self.assertEqual(self.module.WarehouseTransfer.query.count(), 1)

    def test_concurrent_small_transfers_do_not_lose_destination_increments(self):
        clients = [self.client(), self.client()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda c: self.transfer(c, quantity=3).status_code, clients))
        self.assertEqual(results, [200, 200])
        with self.app.app_context():
            self.assertEqual(self.module.WarehouseInventory.query.one().quantity, 4)
            product = self.module.Product.query.filter_by(branch_id=self.b, barcode='PHYSICAL-123').one()
            self.assertEqual(product.stock, 6)

    def test_db_uniqueness_and_empty_barcodes(self):
        client = self.client()
        for index in range(2):
            result = client.post('/api/products', json={'name': f'No barcode {index}', 'price': 1, 'stock': 0, 'barcode': ''})
            self.assertEqual(result.status_code, 201, result.get_json())
        self.assertEqual(self.client(self.b).post('/api/products', json={
            'name': 'Same physical barcode', 'price': 1, 'stock': 0, 'barcode': 'PHYSICAL-123'}).status_code, 201)
        self.assertEqual(client.post('/api/products', json={
            'name': 'Duplicate', 'price': 1, 'stock': 0, 'barcode': 'PHYSICAL-123'}).status_code, 400)

    def test_transfer_fifo_and_same_branch(self):
        m = self.module
        with self.app.app_context():
            item = m.WarehouseInventory.query.one()
            item.quantity = 2
            item.received_date = m.datetime(2020, 1, 1)
            second = m.WarehouseInventory(product_id=self.product, quantity=5, branch_id=self.a,
                                          received_date=m.datetime(2021, 1, 1))
            m.db.session.add(second)
            m.db.session.commit()
        result = self.transfer(quantity=4, target_branch_id=self.a)
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(result.get_json()['target_product_id'], self.product)
        with self.app.app_context():
            self.assertEqual([item.quantity for item in m.WarehouseInventory.query.order_by(m.WarehouseInventory.id)], [0, 3])
            self.assertEqual(m.db.session.get(m.Product, self.product).stock, 9)

    def test_transfer_rolls_back_debit_when_destination_write_fails(self):
        m = self.module
        with patch.object(m.db.session, 'flush', side_effect=RuntimeError('injected')):
            self.assertEqual(self.transfer().status_code, 500)
        with self.app.app_context():
            self.assertEqual(m.WarehouseInventory.query.one().quantity, 10)
            self.assertEqual(m.WarehouseTransfer.query.count(), 0)

    def test_branch_reads_writes_and_stale_tab_are_isolated(self):
        client = self.client()
        self.assertEqual(client.get(f'/api/products/{self.unrelated}').status_code, 404)
        self.assertEqual(client.put(f'/api/products/{self.unrelated}', json={'stock': 999}).status_code, 404)
        self.assertEqual(client.delete(f'/api/products/{self.unrelated}').status_code, 404)
        self.assertEqual(client.get('/api/products', headers={'X-POS-Branch-ID': str(self.b)}).status_code, 409)
        self.assertEqual(self.client(self.b, 'cashier').post('/api/warehouse/transfer', json={}).status_code, 403)
        self.assertEqual(self.client(self.b, 'cashier').get(f'/api/products?branch_id={self.a}').status_code, 403)

    def test_customer_debt_branch_scope_and_split_validation(self):
        m = self.module
        with self.app.app_context():
            customer = m.Customer(name='Beta private', branch_id=self.b)
            m.db.session.add(customer)
            m.db.session.flush()
            debt = m.Debt(customer_id=customer.id, amount=20, balance=20, branch_id=self.b)
            m.db.session.add(debt)
            m.db.session.commit()
            customer_id, debt_id = customer.id, debt.id
        client = self.client()
        self.assertEqual(client.get(f'/api/customers/{customer_id}').status_code, 404)
        self.assertEqual(client.put(f'/api/customers/{customer_id}', json={'name': 'stolen'}).status_code, 404)
        for method in ('get', 'put', 'delete'):
            response = getattr(client, method)(f'/api/debts/{debt_id}', json={'notes': 'stolen'})
            self.assertEqual(response.status_code, 404)
        payload = {'items': [{'product_id': self.product, 'quantity': 1, 'price': 10}],
                   'customer_id': customer_id, 'payment_method': 'debt'}
        self.assertEqual(client.post('/api/sales', json=payload).status_code, 404)
        payload.update(payment_method='split_payment', payment_breakdown={'cash': 5, 'mobile_payment': 5})
        self.assertEqual(client.post('/api/sales', json=payload).status_code, 400)

    def test_receipt_settings_and_historical_receipt_identity_are_isolated(self):
        a, b = self.client(), self.client(self.b)
        for client, name in ((a, 'Alpha Receipt'), (b, 'Beta Receipt')):
            response = client.put('/api/settings', json={'receipt_customization': {'brand_name': name}})
            self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(a.get('/api/settings').get_json()['receipt_customization']['brand_name'], 'Alpha Receipt')
        self.assertEqual(b.get('/api/settings').get_json()['receipt_customization']['brand_name'], 'Beta Receipt')
        sale = a.post('/api/sales', json={'items': [{'product_id': self.product, 'quantity': 1, 'price': 10}],
                                        'payment_method': 'cash', 'cash_received': 10})
        self.assertEqual(sale.status_code, 201, sale.get_json())
        txn = sale.get_json()['transaction_id']
        receipt = a.get(f'/api/sales/{txn}/print')
        self.assertEqual(receipt.status_code, 200)
        self.assertIn(b'Alpha Receipt', receipt.data)
        self.assertNotIn(b'Beta Receipt', receipt.data)
        self.assertEqual(b.get(f'/api/sales/{txn}/print').status_code, 404)

    def test_upgrade_and_second_start_preserve_columns_references_and_categories(self):
        """Exercise real startup twice against both historical UNIQUE forms."""
        m = self.module
        with self.app.app_context():
            m.db.session.remove()
            live_path = m.db.engine.url.database
        for inline in (True, False):
            with self.subTest(inline=inline), tempfile.TemporaryDirectory(prefix='pos-upgrade-') as directory:
                root = Path(directory)
                (root / 'instance').mkdir()
                target = root / 'instance' / 'pos.db'
                with closing(sqlite3.connect(live_path)) as source, closing(sqlite3.connect(target)) as connection:
                    source.backup(connection)
                    connection.executescript('''
                        DROP TABLE IF EXISTS schema_migration;
                        DROP TABLE IF EXISTS transfer_request;
                        DROP TABLE product;
                        CREATE TABLE product (
                            id INTEGER PRIMARY KEY, barcode VARCHAR(50), name VARCHAR(100) NOT NULL,
                            price FLOAT NOT NULL, cost FLOAT, stock INTEGER, category VARCHAR(50),
                            category_id INTEGER REFERENCES category(id), unit_id INTEGER REFERENCES unit(id),
                            tax_rate FLOAT, photo_filename VARCHAR(255), reorder_point INTEGER,
                            reorder_quantity INTEGER, reorder_enabled BOOLEAN, branch_id INTEGER REFERENCES branch(id),
                            extension_note TEXT
                        );
                        DROP TABLE category;
                        CREATE TABLE category (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(50) NOT NULL UNIQUE,
                            description VARCHAR(200), color VARCHAR(7), is_active BOOLEAN,
                            sort_order INTEGER, branch_id INTEGER REFERENCES branch(id),
                            created_at DATETIME, updated_at DATETIME
                        );
                        CREATE TABLE upgrade_trigger_log (product_id INTEGER);
                        CREATE TRIGGER preserve_product_trigger AFTER UPDATE OF stock ON product
                        BEGIN INSERT INTO upgrade_trigger_log VALUES (NEW.id); END;
                        CREATE INDEX preserve_product_extension ON product(extension_note);
                        INSERT INTO category VALUES (77, 'Legacy', 'description', '#123456', 1, 2, 1, NULL, NULL);
                        INSERT INTO category (id, name, branch_id) VALUES (999, 'Deleted high ID', 1);
                        DELETE FROM category WHERE id=999;
                        INSERT INTO unit (id, name, symbol, unit_type, factor_to_base, is_active, sort_order)
                            VALUES (17, 'Test Piece', 'TP', 'count', 1, 1, 0);
                        INSERT INTO product VALUES
                            (501, 'UPGRADE-SCAN', 'Upgrade', 12, 3, 5, 'Legacy', 77, 17, 0,
                             'keep.png', 4, 9, 1, 2, 'preserved extension');
                        INSERT INTO warehouse_inventory (product_id, quantity, branch_id) VALUES (501, 8, 1);
                        INSERT INTO warehouse_transfer
                            (id, product_id, quantity, idempotency_key, request_fingerprint,
                             target_product_id, resulting_stock)
                        VALUES (321, 501, 3, 'upgraded-key', 'preserved-fingerprint', 501, 5);
                    ''')
                    # Rebuild just this fixture to model SQLAlchemy's old inline
                    # constraint, or its alternate standalone unique index.
                    if inline:
                        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='product'").fetchone()[0]
                        sql = sql.replace('CREATE TABLE product', 'CREATE TABLE old_product')
                        sql = sql.rsplit(')', 1)[0] + ', UNIQUE (barcode))'
                        connection.execute(sql)
                        connection.execute('INSERT INTO old_product SELECT * FROM product')
                        connection.execute('DROP TABLE product')
                        connection.execute('ALTER TABLE old_product RENAME TO product')
                        connection.execute('CREATE INDEX preserve_product_extension ON product(extension_note)')
                        connection.executescript('''CREATE TRIGGER preserve_product_trigger AFTER UPDATE OF stock ON product
                            BEGIN INSERT INTO upgrade_trigger_log VALUES (NEW.id); END;''')
                    else:
                        connection.execute('CREATE UNIQUE INDEX arbitrary_legacy_barcode ON product(barcode)')
                    connection.commit()
                script = '''
import sys
from flask import Flask
from unittest.mock import patch
original = Flask.__init__
def isolated(app, *args, **kwargs):
    kwargs.update(instance_path=sys.argv[1] + '/instance', root_path=sys.argv[1])
    original(app, *args, **kwargs)
with patch.object(Flask, '__init__', isolated):
    import app
with app.app.app_context():
    app.db.session.remove()
    app.db.engine.dispose()
'''
                for restart in range(2):
                    result = subprocess.run([sys.executable, '-c', script, str(root)],
                        cwd=Path(__file__).parent, capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    with closing(sqlite3.connect(target)) as connection:
                        row = connection.execute('SELECT barcode, stock, unit_id, extension_note, category_id FROM product WHERE id=501').fetchone()
                        self.assertEqual(row[:4], ('UPGRADE-SCAN', 5, 17, 'preserved extension'))
                        self.assertNotEqual(row[4], 77)
                        self.assertEqual(connection.execute('SELECT branch_id, color FROM category WHERE id=?', (row[4],)).fetchone(), (2, '#123456'))
                        self.assertEqual(connection.execute('SELECT COUNT(*) FROM category').fetchone()[0], 2)
                        self.assertGreaterEqual(connection.execute("SELECT seq FROM sqlite_sequence WHERE name='category'").fetchone()[0], 1000)
                        self.assertEqual(connection.execute('''SELECT fingerprint, target_product_id, resulting_stock, transfer_id
                            FROM transfer_request WHERE key='upgraded-key' ''').fetchone(),
                            ('preserved-fingerprint', 501, 5, 321))
                        self.assertEqual(connection.execute('SELECT COUNT(*) FROM schema_migration').fetchone()[0], 1)
                        self.assertIsNotNone(connection.execute("SELECT name FROM sqlite_master WHERE name='preserve_product_trigger'").fetchone())
                        self.assertIsNotNone(connection.execute("SELECT name FROM sqlite_master WHERE name='preserve_product_extension'").fetchone())
                        self.assertEqual(connection.execute('SELECT product_id FROM warehouse_inventory ORDER BY id DESC LIMIT 1').fetchone()[0], 501)
                        if restart == 1:
                            connection.execute("INSERT INTO product (name, price, barcode, branch_id) VALUES ('Other branch', 1, 'UPGRADE-SCAN', 1)")
                            with self.assertRaises(sqlite3.IntegrityError):
                                connection.execute("INSERT INTO product (name, price, barcode, branch_id) VALUES ('Duplicate', 1, 'UPGRADE-SCAN', 2)")
                backup = Path(str(target) + '.pre-branch-v2.bak')
                self.assertTrue(backup.exists())
                if os.name != 'nt':
                    self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                with closing(sqlite3.connect(backup)) as connection:
                    self.assertNotIn('catalog_id', [row[1] for row in connection.execute('PRAGMA table_info(product)')])

    def test_login_uses_assignment_and_inactive_branch_never_falls_back(self):
        client = self.app.test_client()
        self.assertEqual(client.post('/login', data={'username': 'cashier', 'password': 'password'}).status_code, 302)
        with client.session_transaction() as session:
            self.assertEqual(session['branch_id'], self.b)
        with self.app.app_context():
            self.module.db.session.get(self.module.Branch, self.b).is_active = False
            self.module.db.session.commit()
        self.assertEqual(client.get('/api/products').status_code, 403)
        client.post('/login', data={'username': 'cashier', 'password': 'password'})
        with client.session_transaction() as session:
            self.assertNotIn('user_id', session)


if __name__ == '__main__':
    unittest.main()