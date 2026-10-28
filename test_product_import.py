"""Product import HTTP contracts; run only through the disposable run_tests.py."""
import io
import json
import base64
import os
import unittest
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED
from flask import Flask
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError

import xlsxwriter

def workbook(rows):
    output = io.BytesIO()
    book = xlsxwriter.Workbook(output, {'in_memory': True})
    sheet = book.add_worksheet()
    for index, row in enumerate(rows):
        sheet.write_row(index, 0, row)
    book.close()
    return output.getvalue()


# A 1x1 PNG, the smallest picture a workbook can carry.
PNG_1PX = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=='


def picture_workbook(rows, column=None):
    """Same rows, with a picture pasted into each data row's picture column."""
    output = io.BytesIO()
    book = xlsxwriter.Workbook(output, {'in_memory': True})
    sheet = book.add_worksheet()
    for index, row in enumerate(rows):
        sheet.write_row(index, 0, row)
    photo_column = len(rows[0]) - 1 if column is None else column
    for index in range(1, len(rows)):
        sheet.insert_image(index, photo_column, 'photo.png',
                           {'image_data': io.BytesIO(base64.b64decode(PNG_1PX))})
    book.close()
    return output.getvalue()


class ProductImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='pos-import-')
        root = Path(cls.temporary.name)
        shutil.copytree(Path(__file__).parent / 'templates', root / 'templates')
        original = Flask.__init__

        def isolated_init(application, *args, **kwargs):
            kwargs.update(root_path=str(root), instance_path=str(root / 'instance'))
            original(application, *args, **kwargs)

        spec = importlib.util.spec_from_file_location('_product_import_test_app', Path(__file__).with_name('app.py'))
        cls.m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.m
        with patch.object(Flask, '__init__', isolated_init):
            spec.loader.exec_module(cls.m)
        for name in ('app', 'db', 'Branch', 'Category', 'Product', 'Unit', 'User'):
            globals()[name] = getattr(cls.m, name)
        app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.temporary.cleanup()

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.branch = Branch(name='Local', code='LOCAL', is_default=True)
        self.other = Branch(name='Other', code='OTHER')
        db.session.add_all([self.branch, self.other])
        db.session.flush()
        self.user = User(username='import-manager', password='unused', role='manager', branch_id=self.branch.id)
        self.unit = Unit(name='Piece', symbol='pc', is_active=True)
        db.session.add_all([self.user, self.unit])
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.user.id, role='manager', branch_id=self.branch.id)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def post(self, action, content, **fields):
        data = {'file': (io.BytesIO(content), 'products.xlsx'), 'duplicate_mode': 'skip'}
        data.update(fields)
        return self.client.post('/api/products/import/' + action, data=data)

    def test_template_preview_commit_creates_zero_stock_catalog(self):
        response = self.client.get('/api/products/import/template')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b'PK'))
        # The template offers a Photo column for pasted pictures.
        from product_import import _xlsx
        header = next(cells for number, cells in _xlsx(response.data)[0] if number == 1)
        self.assertEqual([cell[0] for cell in header],
                         ['Name', 'Barcode', 'Price', 'Cost', 'Category', 'Unit', 'Tax Rate', 'Photo'])
        content = workbook([['Name', 'Barcode', 'Price', 'Unit', 'Stock'], ['Tea', '0000123', 12.5, 'pc', 99]])
        response = self.post('preview', content)
        self.assertEqual(response.status_code, 200, response.get_json())
        preview = response.get_json()
        self.assertEqual(preview['preview']['summary'], {'create': 1, 'update': 0, 'skip': 0, 'error': 0})
        self.assertTrue(preview['warnings'])
        self.assertEqual(Product.query.count(), 0)
        result = self.post('commit', content, preview_token=preview['preview_token'])
        self.assertEqual(result.status_code, 200, result.get_json())
        product = Product.query.one()
        self.assertEqual((product.name, product.barcode, product.stock, product.unit_id), ('Tea', '0000123', 0, self.unit.id))
        self.assertEqual(self.post('commit', content, preview_token=preview['preview_token']).status_code, 409)

    def preview(self, rows, **fields):
        content = workbook(rows)
        response = self.post('preview', content, **fields)
        self.assertEqual(response.status_code, 200, response.get_json())
        return content, response.get_json()

    def product(self, **fields):
        defaults = dict(name='Tea', barcode='T', price=10, cost=4, stock=17,
                        photo_filename='keep.png', catalog_id='keep-catalog', branch_id=self.branch.id)
        defaults.update(fields)
        product = Product(**defaults)
        db.session.add(product)
        db.session.commit()
        return product

    def test_updates_only_nonblank_catalog_fields_and_preserves_identity(self):
        product = self.product()
        content, preview = self.preview([['SKU', 'Selling Price', 'Cost Price', 'Name'], ['T', 20, '', 'Renamed']], duplicate_mode='update')
        self.assertEqual(preview['preview']['rows'][0]['action'], 'update')
        self.assertEqual(self.post('commit', content, duplicate_mode='update', preview_token=preview['preview_token']).status_code, 200)
        db.session.expire_all()
        self.assertEqual((product.name, product.price, product.cost, product.stock, product.catalog_id, product.photo_filename),
                         ('Renamed', 20, 4, 17, 'keep-catalog', 'keep.png'))

    def test_unknown_headers_can_be_remapped_and_effective_mapping_is_signed(self):
        content, initial = self.preview([['Description', 'Scan', 'Retail'], ['Tea', '0001', 12]])
        self.assertEqual(initial['columns'], ['Description', 'Scan', 'Retail'])
        self.assertEqual(initial['mapping'], {})
        self.assertEqual(initial['preview']['summary']['error'], 1)
        mapping = {'name': 'Description', 'barcode': 'Scan', 'price': 'Retail'}
        preview = self.post('preview', content, mapping=json.dumps(mapping)).get_json()
        mapping.update(cost='', category='', unit='', tax_rate='')
        self.assertEqual(self.post('commit', content, mapping=json.dumps(mapping), preview_token=preview['preview_token']).status_code, 200)

    def test_branch_scoped_barcode_and_name_only_never_updates(self):
        self.product(branch_id=self.other.id)
        _, preview = self.preview([['Name', 'Barcode', 'Price'], ['Tea', 'T', 12]])
        self.assertEqual(preview['preview']['summary']['create'], 1)
        self.product()
        _, preview = self.preview([['Name', 'Price'], ['  TEA  ', 99]], duplicate_mode='update')
        self.assertEqual(preview['preview']['summary']['skip'], 1)

    def test_collisions_ambiguous_names_and_repeated_identities_error(self):
        self.product()
        for rows in ([['Tea', 'OTHER', 12]], [['Other', 'N', 1], ['Other', 'M', 2]], [['Other', 'N', 1], ['Else', 'N', 2]]):
            with self.subTest(rows=rows):
                _, preview = self.preview([['Name', 'Barcode', 'Price']] + rows)
                self.assertEqual(preview['preview']['summary']['error'], len(rows))
        self.product(name=' TEA ', barcode='T2', catalog_id='different-catalog')
        _, preview = self.preview([['Name', 'Price'], ['Tea', 12]])
        self.assertEqual(preview['preview']['summary']['error'], 1)

    def test_all_errors_block_atomic_category_and_product_writes(self):
        content, preview = self.preview([['Name', 'Price', 'Category'], ['Good', 12, 'New Category'], ['Bad', -1, 'Other Category']], create_categories='true')
        self.assertIn('create active category', preview['preview']['rows'][0]['message'])
        response = self.post('commit', content, create_categories='true', preview_token=preview['preview_token'])
        self.assertEqual(response.status_code, 400)
        self.assertEqual((Product.query.count(), Category.query.count()), (0, 0))

    def test_category_creation_is_opt_in_active_and_normalized(self):
        rows = [['Name', 'Price', 'Category'], ['Tea', 12, 'Beverage'], ['Coffee', 20, ' beverage ']]
        _, preview = self.preview(rows)
        self.assertEqual(preview['preview']['summary']['error'], 2)
        content, preview = self.preview(rows, create_categories='true')
        self.assertEqual(self.post('commit', content, create_categories='true', preview_token=preview['preview_token']).status_code, 200)
        self.assertEqual(Category.query.count(), 1)
        self.assertEqual(len({p.category_id for p in Product.query.all()}), 1)
        category = Category.query.one()
        category.is_active = False
        db.session.commit()
        _, preview = self.preview([['Name', 'Price', 'Category'], ['Juice', 1, 'Beverage']], create_categories='true')
        self.assertEqual(preview['preview']['summary']['error'], 1)

    def test_invalid_unit_numeric_values_and_mapped_formulas(self):
        for field, value in [('Unit', 'missing'), ('Price', 'NaN'), ('Price', 'Infinity'), ('Price', '-1'), ('Tax Rate', 101), ('Cost', -1), ('Name', '=1+1')]:
            with self.subTest(field=field, value=value):
                values = {'Name': 'Tea', 'Price': 12, field: value}
                _, preview = self.preview([list(values), list(values.values())])
                self.assertEqual(preview['preview']['summary']['error'], 1)
        self.unit.is_active = False
        db.session.commit()
        _, preview = self.preview([['Name', 'Price', 'Unit'], ['Tea', 12, 'pc']])
        self.assertEqual(preview['preview']['summary']['error'], 1)
        _, preview = self.preview([['Name', 'Price', 'Ignored'], ['Tea', 12, '=1+1']])
        self.assertEqual(preview['preview']['summary']['create'], 1)

    def test_numeric_barcode_precision_and_zero_padding(self):
        _, preview = self.preview([['Name', 'Barcode', 'Price'], ['Tea', 1234567890123456, 12]])
        self.assertEqual(preview['preview']['summary']['error'], 1)
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = book.add_worksheet()
        sheet.write_row(0, 0, ['Name', 'Barcode', 'Price'])
        sheet.write(1, 0, 'Tea')
        sheet.write_number(1, 1, 123, book.add_format({'num_format': '000000'}))
        sheet.write(1, 2, 12)
        book.close()
        preview = self.post('preview', output.getvalue()).get_json()
        self.assertEqual(preview['preview']['rows'][0]['barcode'], '000123')

    def test_own_export_metadata_and_total_row_are_not_products(self):
        self.product()
        response = self.client.get('/api/products/export?format=xlsx')
        self.assertEqual(response.status_code, 200)
        preview = self.post('preview', response.data).get_json()
        self.assertEqual(preview['mapping']['name'], 'Product Name')
        self.assertEqual(preview['preview']['summary'], {'create': 0, 'update': 0, 'skip': 1, 'error': 0}, preview)

    def test_commit_rejects_token_tamper_expiration_and_request_changes(self):
        content, preview = self.preview([['Name', 'Barcode', 'Price'], ['Tea', 'T', 12]])
        token = preview['preview_token']
        for fields in ({'preview_token': token + 'x'}, {'preview_token': token, 'duplicate_mode': 'update'},
                       {'preview_token': token, 'create_categories': 'true'}, {'preview_token': ''}):
            self.assertEqual(self.post('commit', content, **fields).status_code, 409)
        changed = workbook([['Name', 'Barcode', 'Price'], ['Tea', 'T', 13]])
        self.assertEqual(self.post('commit', changed, preview_token=token).status_code, 409)
        signer = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='product-import-v1')
        payload = signer.loads(token)
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=1):
            expired = signer.dumps(payload)
        self.assertEqual(self.post('commit', content, preview_token=expired).status_code, 409)
        self.assertEqual(Product.query.count(), 0)

    def test_commit_rejects_changed_product_category_unit_or_identity(self):
        content = workbook([['Name', 'Price'], ['New', 1]])
        for kind in ('product', 'category', 'unit', 'user', 'branch'):
            token = self.post('preview', content).get_json()['preview_token']
            if kind == 'product':
                self.product()
            elif kind == 'category':
                db.session.add(Category(name='New cat', branch_id=self.branch.id))
                db.session.commit()
            elif kind == 'unit':
                self.unit.name = 'Changed'
                db.session.commit()
            elif kind == 'user':
                user = User(username='other-manager', password='unused', role='manager', branch_id=self.branch.id)
                db.session.add(user)
                db.session.commit()
                with self.client.session_transaction() as session:
                    session['user_id'] = user.id
            else:
                with self.client.session_transaction() as session:
                    session['branch_id'] = self.other.id
            self.assertEqual(self.post('commit', content, preview_token=token).status_code, 409, kind)

    def test_route_roles_and_global_stale_identity_gate(self):
        content = workbook([['Name', 'Price'], ['Tea', 12]])
        for role, status in [('cashier', 403), ('viewer', 403), ('boss', 200)]:
            with self.client.session_transaction() as session:
                session['role'] = role
            self.assertEqual(self.client.get('/api/products/import/template').status_code, status)
            self.assertEqual(self.post('preview', content).status_code, status)
            self.assertEqual(self.post('commit', content).status_code, 409 if role == 'boss' else status)
        response = self.client.post('/api/products/import/preview', headers={'X-POS-Branch-ID': str(self.other.id)})
        self.assertEqual(response.status_code, 409)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get('/api/products/import/template').status_code, 401)

    def test_parser_limits_and_unsafe_xml_rejected(self):
        for rows in ([['Name'] * 51], [['Name', 'Price']] + [[str(i), 1] for i in range(2001)], [['Name', 'Name', 'Price'], ['Tea', 'Tea', 1]]):
            response = self.post('preview', workbook(rows))
            self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(self.post('preview', b'not a workbook').status_code, 400)
        self.assertIn(self.post('preview', b'x' * (5 * 1024 * 1024 + 1)).status_code, (400, 413))
        source = workbook([['Name', 'Price'], ['Tea', 12]])
        for attack in ('entity', 'expanded'):
            output = io.BytesIO()
            with ZipFile(io.BytesIO(source)) as old, ZipFile(output, 'w', ZIP_DEFLATED) as new:
                for member in old.infolist():
                    value = old.read(member.filename)
                    if attack == 'entity' and member.filename == 'xl/workbook.xml':
                        value = b'<!DOCTYPE x [<!ENTITY evil "hi">]>' + value
                    new.writestr(member.filename, value)
                if attack == 'expanded':
                    new.writestr('huge.xml', b'x' * (11 * 1024 * 1024))
            self.assertEqual(self.post('preview', output.getvalue()).status_code, 400)

    def test_inline_strings_without_shared_table_supported(self):
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'constant_memory': True})
        sheet = book.add_worksheet()
        sheet.write_row(0, 0, ['Name', 'Barcode', 'Price'])
        sheet.write_row(1, 0, ['Tea', '00000000000000001', 12])
        book.close()
        preview = self.post('preview', output.getvalue()).get_json()
        self.assertEqual(preview['preview']['rows'][0]['barcode'], '00000000000000001')
        self.assertEqual(preview['preview']['summary']['create'], 1)

    def test_csv_keeps_text_barcodes_and_rejects_mapped_formulas(self):
        for content, expected in [(b'Name,Barcode,Price\nTea,000123,12\n', 'create'),
                                  (b'Name,Barcode,Price\nTea,000123,=1+1\n', 'error')]:
            response = self.client.post('/api/products/import/preview', data={
                'file': (io.BytesIO(content), 'catalog.csv'), 'duplicate_mode': 'skip'})
            self.assertEqual(response.status_code, 200)
            preview = response.get_json()
            self.assertEqual(preview['preview']['rows'][0]['barcode'], '000123')
            self.assertEqual(preview['preview']['rows'][0]['action'], expected)

    def test_initial_inference_matches_ui_full_mapping_on_commit(self):
        content, preview = self.preview([['Item Name', 'SKU', 'Selling Price'], ['Tea', 'T', 12]])
        mapping = {key: preview['mapping'].get(key, '') for key in
                   ('name', 'barcode', 'price', 'cost', 'category', 'unit', 'tax_rate')}
        response = self.post('commit', content, mapping=json.dumps(mapping), preview_token=preview['preview_token'])
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_database_failure_rolls_back_created_categories_and_all_products(self):
        content, preview = self.preview([['Name', 'Price', 'Category'], ['Tea', 12, 'New']], create_categories='true')
        with patch.object(db.session, 'commit', side_effect=IntegrityError('write failed', {}, Exception())):
            result = self.post('commit', content, create_categories='true', preview_token=preview['preview_token'])
        self.assertEqual(result.status_code, 409)
        self.assertEqual((Product.query.count(), Category.query.count()), (0, 0))

    def test_first_useful_sheet_skips_report_cover(self):
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'in_memory': True})
        book.add_worksheet('Cover').write(0, 0, 'Catalog instructions')
        sheet = book.add_worksheet('Catalog')
        sheet.write_row(0, 0, ['Name', 'Price'])
        sheet.write_row(1, 0, ['Tea', 12])
        book.close()
        result = self.post('preview', output.getvalue()).get_json()
        self.assertEqual(result['preview']['summary']['create'], 1)

    def test_boolean_date_and_percentage_cell_types_are_not_silently_corrupted(self):
        from datetime import datetime
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = book.add_worksheet()
        sheet.write_row(0, 0, ['Name', 'Barcode', 'Price', 'Tax Rate'])
        sheet.write_row(1, 0, ['Boolean barcode', True, 12])
        sheet.write_row(2, 0, ['Boolean price', 'B', True])
        sheet.write(3, 0, 'Date barcode')
        sheet.write_datetime(3, 1, datetime(2025, 1, 1), book.add_format({'num_format': 'yyyy-mm-dd'}))
        sheet.write(3, 2, 12)
        sheet.write_row(4, 0, ['Percent tax', 'P', 12])
        sheet.write_number(4, 3, .12, book.add_format({'num_format': '0%'}))
        book.close()
        result = self.post('preview', output.getvalue()).get_json()
        self.assertEqual(result['preview']['summary']['error'], 3)
        self.assertEqual(result['preview']['rows'][3]['changes']['tax_rate'], 12)
    def test_pictures_pasted_in_a_photo_column_become_product_photos(self):
        content = picture_workbook([['Name', 'Price', 'Photo'], ['Tea', 12, '']])
        before = set(os.listdir(app.config['UPLOAD_FOLDER']))
        preview = self.post('preview', content).get_json()
        self.assertEqual(preview['preview']['summary']['create'], 1, preview)
        self.assertEqual(preview['preview']['rows'][0]['photo'], 'image1.png')
        # Previewing is read-only: no image file may appear before confirmation.
        self.assertEqual(set(os.listdir(app.config['UPLOAD_FOLDER'])), before)

        result = self.post('commit', content, preview_token=preview['preview_token'])
        self.assertEqual(result.status_code, 200, result.get_json())
        product = Product.query.one()
        self.assertTrue(product.photo_filename.endswith('.png'), product.photo_filename)
        self.assertTrue((Path(app.config['UPLOAD_FOLDER']) / product.photo_filename).is_file())

    def test_updating_a_row_replaces_its_photo_and_removes_the_previous_file(self):
        previous = Path(app.config['UPLOAD_FOLDER']) / 'previous.png'
        previous.write_bytes(b'old photo')
        product = self.product(photo_filename='previous.png')
        content = picture_workbook([['SKU', 'Selling Price', 'Photo'], ['T', 20, '']])
        preview = self.post('preview', content, duplicate_mode='update').get_json()
        self.assertEqual(preview['preview']['rows'][0]['action'], 'update', preview)
        result = self.post('commit', content, duplicate_mode='update',
                           preview_token=preview['preview_token'])
        self.assertEqual(result.status_code, 200, result.get_json())
        db.session.expire_all()
        self.assertNotEqual(product.photo_filename, 'previous.png')
        self.assertFalse(previous.exists())

    def test_pictures_without_a_photo_column_are_reported_not_attached(self):
        content = picture_workbook([['Name', 'Price', 'Notes'], ['Tea', 12, '']])
        preview = self.post('preview', content).get_json()
        self.assertNotIn('photo', preview['preview']['rows'][0])
        self.assertTrue(any('Pictures found in the workbook' in warning
                            for warning in preview['warnings']), preview['warnings'])
        self.assertEqual(self.post('commit', content,
                                   preview_token=preview['preview_token']).status_code, 200)
        self.assertIsNone(Product.query.one().photo_filename)
