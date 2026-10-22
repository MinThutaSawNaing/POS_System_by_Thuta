"""Clothing HTTP contract against a disposable database (no app.py import)."""

from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
import tempfile
import unittest

from flask import Flask, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect

from clothing import migrate_clothing, register_clothing


class ClothingCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='clothing-catalog-')
        cls.app = Flask(__name__)
        cls.app.config.update(TESTING=True, SECRET_KEY='test-only',
            SQLALCHEMY_DATABASE_URI='sqlite:///' + str(Path(cls.temporary.name) / 'test.sqlite'))
        cls.db = db = SQLAlchemy(cls.app)

        class Product(db.Model):
            __tablename__ = 'product'
            __table_args__ = (db.UniqueConstraint('branch_id', 'catalog_id'),
                              db.UniqueConstraint('branch_id', 'barcode'))
            id = db.Column(db.Integer, primary_key=True)
            catalog_id = db.Column(db.String(64))
            branch_id = db.Column(db.Integer)
            name = db.Column(db.String(100), nullable=False)
            barcode = db.Column(db.String(50))
            price = db.Column(db.Float, nullable=False)
            cost = db.Column(db.Float)
            stock = db.Column(db.Integer, default=0)
            tax_rate = db.Column(db.Float, default=0)
            unit_id = db.Column(db.Integer)
            photo_filename = db.Column(db.String(255))

        def login_required(fn):
            @wraps(fn)
            def decorated(*args, **kwargs):
                if 'user_id' not in session:
                    return jsonify(error='Unauthorized'), 401
                return fn(*args, **kwargs)
            return decorated

        def manager_required(fn):
            @wraps(fn)
            def decorated(*args, **kwargs):
                if session.get('role') not in ('manager', 'boss'):
                    return jsonify(error='Manager access required'), 403
                return fn(*args, **kwargs)
            return decorated

        cls.Product = Product
        cls.models = register_clothing(cls.app, db, dict(Product=Product,
            get_current_branch_id=lambda: session.get('branch_id'),
            login_required=login_required, manager_or_boss_required=manager_required))

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.engine.dispose()
        cls.temporary.cleanup()

    def setUp(self):
        with self.app.app_context():
            self.db.drop_all()
            self.db.create_all()
        self.client = self.client_for()
        self.previews = {}
        self.style = self.client.post('/api/clothing/styles', json={
            'name': 'Cotton Shirt', 'code': 'SHIRT', 'brand': 'Local'}).get_json()['style']['id']

    def client_for(self, branch=1, role='manager', user=7):
        client = self.app.test_client()
        with client.session_transaction() as state:
            state.update(user_id=user, role=role, branch_id=branch)
        return client

    def row(self, **changes):
        row = dict(style_id=self.style, size='M', color='Blue', barcode='SH-M-B', price='25.00', cost='9.50')
        row.update(changes)
        return row

    def create(self, **changes):
        row = self.row(**changes)
        row.pop('style_id')
        return self.client.post(f'/api/clothing/styles/{self.style}/variants', json=row)

    def import_rows(self, rows, key='import-1', client=None):
        cache_key = repr(rows)
        if cache_key not in self.previews:
            preview = self.client.post('/api/clothing/import/preview', json={'rows': rows}).get_json()
            self.previews[cache_key] = preview
        preview = self.previews[cache_key]
        return (client or self.client).post('/api/clothing/import/commit', json=dict(
            rows=preview.get('rows') or rows, preview_token=preview.get('preview_token'), idempotency_key=key))

    def products(self):
        with self.app.app_context():
            return [(p.id, p.catalog_id, p.barcode, p.stock) for p in self.Product.query.order_by(self.Product.id).all()]

    def existing(self, branch=1, catalog='legacy-catalog', barcode='LEGACY', stock=8):
        with self.app.app_context():
            product = self.Product(branch_id=branch, catalog_id=catalog, barcode=barcode,
                                   name='Original legacy name', price=25, cost=9.5, stock=stock)
            self.db.session.add(product)
            self.db.session.commit()
            return product.id

    def test_create_keeps_product_identity_and_stock_zero(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        variant = response.get_json()['variant']
        self.assertEqual(variant['stock'], 0)
        self.assertEqual(variant['style_name'], 'Cotton Shirt')
        self.assertEqual(variant['id'], variant['product_id'])
        self.assertEqual(self.products(), [(variant['id'], variant['catalog_id'], 'SH-M-B', 0)])
        listed = self.client.get(f'/api/clothing/styles/{self.style}/variants').get_json()['variants']
        self.assertEqual(listed[0]['id'], variant['id'])

    def test_variant_normalized_uniqueness_includes_archived(self):
        first = self.create(size=' Ｍ ', color='  BLUE ').get_json()['variant']
        self.assertEqual(self.client.delete(f"/api/clothing/variants/{first['variant_id']}").status_code, 200)
        self.assertEqual(self.create(size='m', color='blue', barcode='SECOND').status_code, 400)
        self.assertEqual(len(self.products()), 1)
        self.assertEqual(self.client.get(f'/api/clothing/styles/{self.style}/variants').get_json()['variants'], [])
        rows = self.client.get(f'/api/clothing/styles/{self.style}/variants?include_archived=true').get_json()['variants']
        self.assertTrue(rows[0]['archived'])

    def test_archive_never_deletes_or_mutates_product(self):
        variant = self.create().get_json()['variant']
        before = self.products()
        response = self.client.put(f"/api/clothing/variants/{variant['variant_id']}", json={'size': 'XL'})
        self.assertEqual(response.status_code, 400)
        response = self.client.delete(f'/api/clothing/styles/{self.style}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.products(), before)
        self.assertEqual(self.client.get(f'/api/clothing/styles/{self.style}/variants').get_json()['variants'], [])

    def test_style_validation_and_code_uniqueness(self):
        response = self.client.post('/api/clothing/styles', json={'name': 'Other', 'code': ' shirt '})
        self.assertEqual(response.status_code, 400)
        for changes in ({'name': ''}, {'archived': 'false'}, {'photo_filename': '../secret'}, {'stock': 5}):
            self.assertEqual(self.client.put(f'/api/clothing/styles/{self.style}', json=changes).status_code, 400)
        self.assertEqual(self.client.put(f'/api/clothing/styles/{self.style}', json={'season': 'Summer'}).status_code, 200)

    def test_attach_preserves_existing_product_and_all_branch_clones(self):
        pid = self.existing()
        foreign = self.existing(branch=2, stock=3)
        before = self.products()
        response = self.client.post(f'/api/clothing/styles/{self.style}/attach', json={'product_id': pid, 'size': 'L', 'color': 'White'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.products(), before)
        response = self.client_for(branch=2).get(f'/api/clothing/styles/{self.style}/variants')
        self.assertEqual(response.get_json()['variants'][0]['id'], foreign)
        self.assertEqual(response.get_json()['variants'][0]['stock'], 3)

    def test_attach_is_explicit_branch_scoped_and_immutable(self):
        foreign = self.existing(branch=2)
        path = f'/api/clothing/styles/{self.style}/attach'
        self.assertEqual(self.client.post(path, json={'product_id': foreign, 'size': 'L', 'color': 'White'}).status_code, 400)
        pid = self.existing(catalog='local', barcode='LOCAL')
        self.assertEqual(self.create(product_id=pid).status_code, 400)
        self.assertEqual(self.client.post(path, json={'product_id': pid, 'size': 'L', 'color': 'White', 'name': 'Renamed'}).status_code, 400)

    def test_barcode_conflicts_across_branches_and_catalog_drift(self):
        self.existing(branch=2, barcode='sh-m-b')
        self.assertEqual(self.create().status_code, 400)
        pid = self.existing(barcode='ORIGINAL', catalog='same')
        self.existing(branch=2, barcode='DRIFT', catalog='same')
        response = self.client.post(f'/api/clothing/styles/{self.style}/attach', json={'product_id': pid, 'size': 'L', 'color': 'White'})
        self.assertEqual(response.status_code, 400)

    def test_preview_all_rows_errors_and_never_writes(self):
        rows = [self.row(), self.row(size='L', barcode='DUP', price='NaN'), self.row(size='XL', stock=4)]
        response = self.client.post('/api/clothing/import/preview', json={'rows': rows})
        self.assertEqual(response.status_code, 200)
        preview = response.get_json()
        self.assertFalse(preview['valid'])
        self.assertEqual([e['row'] for e in preview['errors']], [2, 3])
        self.assertEqual(self.products(), [])
        self.assertEqual(self.import_rows(rows).status_code, 400)
        self.assertEqual(self.products(), [])

    def test_csv_preview_explicit_style_no_grouping(self):
        csv = f'style_id,size,color,barcode,price\n{self.style},M,Blue,SCAN-1,25.00\n'
        response = self.client.post('/api/clothing/import/preview', json={'csv': csv})
        self.assertTrue(response.get_json()['valid'])
        preview = response.get_json()
        response = self.client.post('/api/clothing/import/commit', json={
            'rows': preview['rows'], 'preview_token': preview['preview_token'], 'idempotency_key': 'csv-1'})
        self.assertEqual(response.status_code, 201)
        response = self.client.post('/api/clothing/import/preview', json={'csv': 'name,size,color,barcode,price\nShirt,L,Red,R,10\n'})
        self.assertFalse(response.get_json()['valid'])
        self.assertEqual(len(self.products()), 1)

    def test_import_revalidates_after_preview_and_rolls_back_whole_batch(self):
        rows = [self.row(size='L', barcode='L'), self.row()]
        preview = self.client.post('/api/clothing/import/preview', json={'rows': rows}).get_json()
        self.assertTrue(preview['valid'])
        self.previews[repr(rows)] = preview
        self.create()
        before = self.products()
        self.assertEqual(self.import_rows(rows).status_code, 400)
        self.assertEqual(self.products(), before)

    def test_import_replay_durable_branch_user_safe(self):
        rows = [self.row(), self.row(size='L', barcode='L')]
        result = self.import_rows(rows)
        self.assertEqual(result.status_code, 201)
        before = self.products()
        replay = self.import_rows(rows, client=self.client_for())
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()['replayed'])
        self.assertEqual(replay.get_json()['variants'], result.get_json()['variants'])
        self.assertEqual(self.import_rows([self.row(size='XL', barcode='XL')]).status_code, 409)
        self.assertEqual(self.import_rows(rows, client=self.client_for(user=8)).status_code, 400)
        self.assertEqual(self.import_rows(rows, client=self.client_for(branch=2)).status_code, 400)
        self.assertEqual(self.products(), before)

    def test_concurrent_import_same_key_creates_once(self):
        rows = [self.row()]
        self.previews[repr(rows)] = self.client.post('/api/clothing/import/preview', json={'rows': rows}).get_json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: self.import_rows(rows, client=self.client_for()).status_code, range(2)))
        self.assertEqual(sorted(statuses), [200, 201])
        self.assertEqual(len(self.products()), 1)

    def test_preview_token_required_and_cannot_change_reviewed_batch(self):
        rows = [self.row()]
        self.assertEqual(self.client.post('/api/clothing/import/commit', json={
            'rows': rows, 'idempotency_key': 'no-preview'}).status_code, 400)
        preview = self.client.post('/api/clothing/import/preview', json={'rows': rows}).get_json()
        preview['rows'][0]['price'] = '1.00'
        response = self.client.post('/api/clothing/import/commit', json={
            'rows': preview['rows'], 'preview_token': preview['preview_token'], 'idempotency_key': 'changed'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.products(), [])

    def test_cashier_variant_read_omits_cost_and_cross_branch_inventory(self):
        variant = self.create().get_json()['variant']
        cashier = self.client_for(role='cashier')
        response = cashier.get(f"/api/clothing/variants/{variant['variant_id']}")
        self.assertNotIn('cost', response.get_json()['variant'])
        foreign = self.client_for(branch=2, role='cashier')
        self.assertEqual(foreign.get(f"/api/clothing/variants/{variant['variant_id']}").status_code, 404)

    def test_invalid_money_stock_and_payload_are_rejected(self):
        for changes in ({'price': True}, {'price': '-1'}, {'price': '1.234'}, {'price': 'Infinity'},
                        {'cost': 'NaN'}, {'stock': 5}, {'stock': True}, {'size': ''}, {'barcode': ''}):
            with self.subTest(changes=changes):
                self.assertEqual(self.create(**changes).status_code, 400)
        self.assertEqual(self.client.post('/api/clothing/import/preview', json={'rows': []}).status_code, 400)
        self.assertEqual(self.client.post('/api/clothing/import/commit', json={'rows': [self.row()]}).status_code, 400)
        self.assertEqual(self.products(), [])

    def test_authorization_and_branch_scope(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get('/api/clothing/styles').status_code, 401)
        cashier = self.client_for(role='cashier')
        self.assertEqual(cashier.get('/api/clothing/styles').status_code, 200)
        self.assertEqual(cashier.post('/api/clothing/styles', json={'name': 'Denied'}).status_code, 403)
        for path in ('preview', 'commit'):
            self.assertEqual(cashier.post('/api/clothing/import/' + path, json={'rows': [self.row()]}).status_code, 403)
        self.assertEqual(self.client.post(f'/api/clothing/styles/{self.style}/variants', json=self.row(branch_id=2)).status_code, 400)
        self.assertEqual(self.client.get(f'/api/clothing/styles/{self.style}/variants?branch_id=2').status_code, 400)
        self.create()
        self.assertEqual(self.client_for(branch=2).get(f'/api/clothing/styles/{self.style}/variants').get_json()['variants'], [])

    def test_additive_migration_repeatable_does_not_change_product_columns_or_rows(self):
        self.existing()
        before = self.products()
        with self.app.app_context():
            columns = [c['name'] for c in inspect(self.db.engine).get_columns('product')]
            for model in reversed(list(self.models.values())):
                model.__table__.drop(self.db.engine)
            migrate_clothing(self.db, self.models)
            migrate_clothing(self.db, self.models)
            self.assertEqual([c['name'] for c in inspect(self.db.engine).get_columns('product')], columns)
        self.assertEqual(self.products(), before)


if __name__ == '__main__':
    unittest.main()