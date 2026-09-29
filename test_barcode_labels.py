"""Tests for the thermal barcode label sheet (32x19mm, 3-up, 3mm gaps)."""

import re
import unittest
import uuid


class LabelBarcodeSvgTests(unittest.TestCase):
    def test_svg_root_is_clean_and_physical_mm(self):
        from app import build_label_barcode_svg
        svg = build_label_barcode_svg('123456789012')
        self.assertTrue(svg.startswith('<svg'))
        self.assertNotIn('<?xml', svg)
        self.assertIn('viewBox', svg)
        width = float(re.search(r'width="([\d.]+)mm"', svg).group(1))
        height = float(re.search(r'height="([\d.]+)mm"', svg).group(1))
        self.assertLessEqual(width, 29.01)
        self.assertAlmostEqual(height, 8.0, delta=0.05)

    def test_long_barcodes_are_scaled_to_fit_the_label(self):
        from app import build_label_barcode_svg
        long_code = '9' * 40
        svg = build_label_barcode_svg(long_code)
        width = float(re.search(r'width="([\d.]+)mm"', svg).group(1))
        self.assertLessEqual(width, 29.01)
        self.assertGreater(width, 20.0)  # still uses the available space

    def test_empty_value_falls_back_without_crashing(self):
        from app import build_label_barcode_svg
        svg = build_label_barcode_svg(None)
        self.assertTrue(svg.startswith('<svg'))


class LabelSheetRouteTests(unittest.TestCase):
    def setUp(self):
        from app import Product, User, app, db, get_default_branch_id
        self.app = app
        self.db = db
        app.config.update(TESTING=True)
        self.context = app.app_context()
        self.context.push()
        self.user = User.query.filter_by(username='admin').first()
        self.branch_id = get_default_branch_id()
        self.products = []
        for index in range(2):
            product = Product(
                name=f'Label Test Product {index}', price=1500, stock=0,
                tax_rate=0, branch_id=self.branch_id,
                barcode='LBL-' + uuid.uuid4().hex[:12],
            )
            db.session.add(product)
            self.products.append(product)
        db.session.commit()

    def tearDown(self):
        for product in self.products:
            self.db.session.delete(self.db.session.get(type(product), product.id))
        self.db.session.commit()
        self.context.pop()

    def client(self, authenticated=True):
        client = self.app.test_client()
        if authenticated:
            with client.session_transaction() as session:
                session['user_id'] = self.user.id
                session['role'] = 'manager'
                session['branch_id'] = self.branch_id
        return client

    def post_labels(self, **kwargs):
        data = {
            'product_ids': ','.join(str(p.id) for p in self.products),
            'quantities': '{}',
        }
        data.update(kwargs)
        return self.client().post('/api/products/barcode_labels/print', data=data)

    def test_requires_login(self):
        response = self.app.test_client().post(
            '/api/products/barcode_labels/print', data={'product_ids': '1'})
        self.assertEqual(response.status_code, 401)

    def test_rejects_empty_selection(self):
        response = self.client().post(
            '/api/products/barcode_labels/print', data={'product_ids': ''})
        self.assertEqual(response.status_code, 400)

    def test_unknown_products_404(self):
        response = self.client().post(
            '/api/products/barcode_labels/print', data={'product_ids': '99999998,99999999'})
        self.assertEqual(response.status_code, 404)

    def test_sheet_geometry_and_content(self):
        response = self.post_labels()
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        # page = one row of the roll: 3*32 + 2*3 = 102mm wide, 19+3 = 22mm pitch
        self.assertIn('size: 102mm 22mm', html)
        self.assertIn('repeat(3, 32mm)', html)
        self.assertIn('column-gap: 3mm', html)
        self.assertIn('<svg', html)
        for product in self.products:
            self.assertIn(product.name, html)
        # 2 labels -> 1 row with one filler cell
        self.assertEqual(html.count('class="label-row"'), 1)
        self.assertEqual(html.count('class="label label--empty"'), 1)
        self.assertIn('const autoPrint = false', html)

    def test_quantities_expand_into_rows(self):
        import json
        quantities = json.dumps({str(self.products[0].id): 4, str(self.products[1].id): 3})
        response = self.post_labels(quantities=quantities)
        html = response.get_data(as_text=True)
        # 7 labels -> ceil(7/3) = 3 rows
        self.assertEqual(html.count('class="label-row"'), 3)
        self.assertEqual(html.count(self.products[0].name), 4)
        self.assertEqual(html.count(self.products[1].name), 3)

    def test_bad_quantities_fall_back_to_one(self):
        response = self.post_labels(quantities='not-json{{{')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count('class="label-row"'), 1)  # 2 labels -> 1 row

    def test_autoprint_flag(self):
        data = {'product_ids': str(self.products[0].id), 'quantities': '{}'}
        response = self.client().post('/api/products/barcode_labels/print?autoprint=1', data=data)
        self.assertIn('const autoPrint = true', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
