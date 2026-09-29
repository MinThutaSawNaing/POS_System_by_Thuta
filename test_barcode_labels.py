"""Tests for the thermal barcode label sheet (32x19mm, 3-up, 3mm gaps)."""

import re
import unittest
import uuid


class BarcodeLabelSearchMarkupTests(unittest.TestCase):
    """The label picker exposes an accessible, state-preserving live search."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.dashboard = Path('templates/dashboard.html').read_text(encoding='utf-8')

    def test_search_controls_and_live_result_status_are_present(self):
        self.assertIn('id="barcode-product-search"', self.dashboard)
        self.assertIn('type="search"', self.dashboard)
        self.assertIn('oninput="filterBarcodeLabelProducts()"', self.dashboard)
        self.assertIn('id="clear-barcode-product-search"', self.dashboard)
        self.assertIn('id="barcode-product-result-count"', self.dashboard)
        self.assertIn('aria-live="polite"', self.dashboard)

    def test_rows_are_searchable_by_name_barcode_and_category(self):
        self.assertIn('row.dataset.searchText', self.dashboard)
        self.assertIn('product.name', self.dashboard)
        self.assertIn('product.barcode', self.dashboard)
        self.assertIn('product.category', self.dashboard)
        self.assertIn('function filterBarcodeLabelProducts()', self.dashboard)

    def test_filter_hides_rows_without_rebuilding_them(self):
        # Toggling hidden preserves checked state and edited quantities.
        self.assertIn('row.hidden = !matches;', self.dashboard)
        self.assertIn('function clearBarcodeLabelSearch()', self.dashboard)
        self.assertIn('No products match your search.', self.dashboard)

    def test_select_all_operates_on_visible_rows(self):
        self.assertIn('getVisibleBarcodeProductRows()', self.dashboard)
        self.assertIn('row.querySelector(".product-checkbox")', self.dashboard)


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


class LabelGeometrySettingsTests(unittest.TestCase):
    """get_label_geometry, the settings API round-trip and dynamic sheets.

    Every test restores the original setting values so the live database is
    left exactly as it was found.
    """

    def setUp(self):
        from app import AppSetting, User, app, db, get_default_branch_id
        self.app = app
        self.db = db
        app.config.update(TESTING=True)
        self.context = app.app_context()
        self.context.push()
        self.user = User.query.filter_by(username='admin').first()
        self.branch_id = get_default_branch_id()
        self.keys = ('label_width_mm', 'label_height_mm', 'label_gap_mm', 'label_columns')
        self.originals = {
            key: (AppSetting.query.filter_by(key=key).first().value
                  if AppSetting.query.filter_by(key=key).first() else None)
            for key in self.keys
        }

    def tearDown(self):
        from app import AppSetting
        for key, value in self.originals.items():
            setting = AppSetting.query.filter_by(key=key).first()
            if value is None:
                if setting:
                    self.db.session.delete(setting)
            elif setting:
                setting.value = value
            else:
                self.db.session.add(AppSetting(key=key, value=value))
        self.db.session.commit()
        self.context.pop()

    def manager_client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user.id
            session['role'] = 'manager'
            session['branch_id'] = self.branch_id
        return client

    def test_normalize_rejects_junk_and_out_of_range(self):
        from app import normalize_label_setting
        self.assertIsNone(normalize_label_setting('label_width_mm', 'abc'))
        self.assertIsNone(normalize_label_setting('label_width_mm', None))
        self.assertIsNone(normalize_label_setting('label_width_mm', 9))    # below 10
        self.assertIsNone(normalize_label_setting('label_width_mm', 201))  # above 200
        self.assertIsNone(normalize_label_setting('label_gap_mm', -1))
        self.assertIsNone(normalize_label_setting('label_columns', 0))
        self.assertIsNone(normalize_label_setting('unknown_key', 5))
        self.assertEqual(normalize_label_setting('label_width_mm', '37.25'), 37.2)
        self.assertEqual(normalize_label_setting('label_columns', 2.9), 2)
        self.assertEqual(normalize_label_setting('label_gap_mm', 0), 0.0)

    def test_defaults_match_the_physical_roll(self):
        from app import get_label_geometry
        geometry = get_label_geometry()
        self.assertEqual(geometry['label_width_mm'], 32.0)
        self.assertEqual(geometry['label_columns'], 3)
        self.assertEqual(geometry['page_width_mm'], 102.0)
        self.assertEqual(geometry['row_pitch_mm'], 22.0)

    def test_settings_api_round_trip_and_validation(self):
        client = self.manager_client()
        response = client.get('/api/settings')
        self.assertEqual(response.status_code, 200)
        self.assertIn('label_geometry', response.get_json())

        saved = client.put('/api/settings', json={'label_geometry': {
            'label_width_mm': 40, 'label_height_mm': 25,
            'label_gap_mm': 2, 'label_columns': 2,
        }})
        self.assertEqual(saved.status_code, 200)
        echoed = saved.get_json()['label_geometry']
        self.assertEqual(echoed['label_width_mm'], 40.0)
        self.assertEqual(echoed['label_columns'], 2)
        self.assertEqual(echoed['page_width_mm'], 82.0)

        invalid = client.put('/api/settings', json={'label_geometry': {'label_width_mm': 9999}})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('Invalid label', invalid.get_json()['message'])
        # the rejected save must not change the stored value
        from app import get_label_geometry
        self.assertEqual(get_label_geometry()['label_width_mm'], 40.0)

    def test_print_route_follows_configured_geometry(self):
        from app import Product, set_setting
        import uuid
        set_setting('label_columns', '2')
        set_setting('label_width_mm', '40')
        product = Product(
            name='Geometry Test Product', price=1000, stock=0, tax_rate=0,
            branch_id=self.branch_id, barcode='GEO-' + uuid.uuid4().hex[:10],
        )
        self.db.session.add(product)
        self.db.session.commit()
        try:
            response = self.manager_client().post(
                '/api/products/barcode_labels/print',
                data={'product_ids': str(product.id), 'quantities': '{}'},
            )
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('size: 83mm 22mm', html)          # 2*40 + 3 gap
            self.assertIn('repeat(2, 40mm)', html)
            self.assertIn('justify-content: center', html)  # content is centered
            self.assertIn('max-width: 38mm', html)          # barcode fits inside
        finally:
            self.db.session.delete(self.db.session.get(Product, product.id))
            self.db.session.commit()


if __name__ == '__main__':
    unittest.main()
