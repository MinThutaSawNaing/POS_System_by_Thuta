"""API and model tests for the dynamic unit-of-measurement system.

Covers the seeded default units, manager-only unit CRUD, the conversion
relationship math (units only convert inside their own type group), and the
product integration (assign/validate/block-delete-in-use).
"""

import unittest
import uuid

from app import app, convert_unit_quantity, db, Branch, Product, Unit, User


class UnitSystemTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self._ctx = app.app_context()
        self._ctx.push()
        self.branch = Branch.query.filter_by(is_active=True).first()
        self.user = User.query.filter_by(username='admin').first()
        self.unit_ids = []
        self.product_ids = []

    def tearDown(self):
        Product.query.filter(Product.id.in_(self.product_ids or [0])).delete(
            synchronize_session=False
        )
        # Children first so base links never dangle mid-cleanup.
        for unit in Unit.query.filter(Unit.id.in_(self.unit_ids or [0])).all():
            unit.base_unit_id = None
        db.session.commit()
        Unit.query.filter(Unit.id.in_(self.unit_ids or [0])).delete(
            synchronize_session=False
        )
        db.session.commit()
        self._ctx.pop()

    def _client(self, role='manager'):
        client = app.test_client()
        with client.session_transaction() as client_session:
            client_session['user_id'] = self.user.id
            client_session['role'] = role
            client_session['branch_id'] = self.branch.id
        return client

    def _unit_by_symbol(self, symbol):
        return Unit.query.filter_by(symbol=symbol).first()

    def _create_unit(self, **overrides):
        suffix = uuid.uuid4().hex[:6]
        payload = {
            'name': f'Test Unit {suffix}',
            'symbol': f'tu{suffix}',
            'unit_type': 'weight',
            'base_unit_id': None,
            'factor_to_base': 1.0,
            'is_active': True,
        }
        payload.update(overrides)
        unit = Unit(**payload)
        db.session.add(unit)
        db.session.commit()
        self.unit_ids.append(unit.id)
        return unit

    def _create_product(self, unit_id=None):
        product = Product(
            name=f'Unit Test Product {uuid.uuid4().hex}', price=10.0, stock=5,
            tax_rate=0.0, branch_id=self.branch.id, unit_id=unit_id,
        )
        db.session.add(product)
        db.session.commit()
        self.product_ids.append(product.id)
        return product

    # --- Seeded defaults ---

    def test_default_units_are_seeded_with_relationships(self):
        gram = self._unit_by_symbol('g')
        kilogram = self._unit_by_symbol('kg')
        pound = self._unit_by_symbol('lb')
        unit = self._unit_by_symbol('unit')
        self.assertIsNotNone(gram)
        self.assertIsNotNone(kilogram)
        self.assertIsNotNone(pound)
        self.assertIsNotNone(unit)
        self.assertIsNone(gram.base_unit_id)
        self.assertEqual(gram.unit_type, 'weight')
        self.assertEqual(kilogram.base_unit_id, gram.id)
        self.assertEqual(float(kilogram.factor_to_base), 1000.0)
        self.assertEqual(pound.base_unit_id, gram.id)
        self.assertIsNone(unit.base_unit_id)
        self.assertEqual(unit.unit_type, 'count')

    # --- Permissions ---

    def test_units_list_requires_login(self):
        anonymous = app.test_client()
        self.assertEqual(anonymous.get('/api/units').status_code, 401)

    def test_cashier_can_read_but_not_change_units(self):
        cashier = self._client(role='cashier')
        self.assertEqual(cashier.get('/api/units').status_code, 200)
        response = cashier.post('/api/units', json={
            'name': 'Cashier Ton', 'symbol': 'ctn', 'unit_type': 'weight',
        })
        self.assertEqual(response.status_code, 403)

    # --- Validation ---

    def test_manager_can_create_unit_linked_to_base(self):
        gram = self._unit_by_symbol('g')
        client = self._client()
        suffix = uuid.uuid4().hex[:6]
        response = client.post('/api/units', json={
            'name': f'Metric Ton {suffix}', 'symbol': f't{suffix}',
            'unit_type': 'weight', 'base_unit_id': gram.id,
            'factor_to_base': 1000000.0,
        })
        self.assertEqual(response.status_code, 201)
        created = response.get_json()['unit']
        self.unit_ids.append(created['id'])
        self.assertEqual(created['base_unit_symbol'], 'g')
        self.assertEqual(created['factor_to_base'], 1000000.0)

    def test_duplicate_symbol_is_rejected(self):
        client = self._client()
        response = client.post('/api/units', json={
            'name': 'Fake Kilogram', 'symbol': 'KG', 'unit_type': 'weight',
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('already used', response.get_json()['message'])

    def test_base_unit_must_share_the_type(self):
        gram = self._unit_by_symbol('g')
        client = self._client()
        response = client.post('/api/units', json={
            'name': f'Broken {uuid.uuid4().hex[:6]}', 'symbol': f'br{uuid.uuid4().hex[:4]}',
            'unit_type': 'count', 'base_unit_id': gram.id, 'factor_to_base': 5.0,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('same unit type', response.get_json()['message'])

    def test_invalid_factors_are_rejected(self):
        gram = self._unit_by_symbol('g')
        client = self._client()
        for factor in (0, -3, 'abc'):
            response = client.post('/api/units', json={
                'name': f'Bad {uuid.uuid4().hex[:6]}', 'symbol': f'bd{uuid.uuid4().hex[:4]}',
                'unit_type': 'weight', 'base_unit_id': gram.id, 'factor_to_base': factor,
            })
            self.assertEqual(response.status_code, 400)
        # A base unit must convert 1:1 to itself.
        response = client.post('/api/units', json={
            'name': f'Bad Base {uuid.uuid4().hex[:6]}', 'symbol': f'bb{uuid.uuid4().hex[:4]}',
            'unit_type': 'weight', 'factor_to_base': 2.5,
        })
        self.assertEqual(response.status_code, 400)

    # --- Conversion relationships ---

    def test_convert_between_related_units(self):
        client = self._client()
        gram = self._unit_by_symbol('g')
        kilogram = self._unit_by_symbol('kg')
        pound = self._unit_by_symbol('lb')
        ounce = self._unit_by_symbol('oz')

        response = client.get(
            f'/api/units/convert?from_id={kilogram.id}&to_id={gram.id}&quantity=2'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['converted'], 2000.0)

        response = client.get(
            f'/api/units/convert?from_id={pound.id}&to_id={ounce.id}&quantity=1'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['converted'], 16.0)

    def test_convert_unrelated_units_is_rejected(self):
        client = self._client()
        kilogram = self._unit_by_symbol('kg')
        unit = self._unit_by_symbol('unit')
        response = client.get(
            f'/api/units/convert?from_id={kilogram.id}&to_id={unit.id}&quantity=1'
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('not related', response.get_json()['message'])

    def test_convert_requires_login_and_valid_ids(self):
        anonymous = app.test_client()
        self.assertEqual(anonymous.get('/api/units/convert?from_id=1&to_id=2').status_code, 401)
        client = self._client()
        self.assertEqual(client.get('/api/units/convert?from_id=999999&to_id=1').status_code, 400)

    # --- Product integration ---

    def test_product_create_and_update_with_unit(self):
        client = self._client()
        kilogram = self._unit_by_symbol('kg')
        gram = self._unit_by_symbol('g')
        name = f'Rice {uuid.uuid4().hex}'
        response = client.post('/api/products', json={
            'name': name, 'price': 2.5, 'stock': 10, 'unit_id': kilogram.id,
        })
        self.assertEqual(response.status_code, 201)
        # Register the product for cleanup immediately: if the assertions
        # below fail, tearDown must still remove it from the dev database.
        created_product = Product.query.filter_by(name=name).first()
        self.assertIsNotNone(created_product)
        self.product_ids.append(created_product.id)

        listing = client.get(f'/api/products?q={name}').get_json()
        created = listing['items'] if isinstance(listing, dict) else listing
        self.assertEqual(len(created), 1)
        product_id = created[0]['id']
        self.assertEqual(product_id, created_product.id)
        self.assertEqual(created[0]['unit_symbol'], 'kg')

        single = client.get(f'/api/products/{product_id}').get_json()
        self.assertEqual(single['unit_id'], kilogram.id)
        self.assertEqual(single['unit_name'], 'Kilogram')

        response = client.put(f'/api/products/{product_id}', json={'unit_id': gram.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            client.get(f'/api/products/{product_id}').get_json()['unit_symbol'], 'g'
        )

        response = client.put(f'/api/products/{product_id}', json={'unit_id': ''})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(client.get(f'/api/products/{product_id}').get_json()['unit_id'])

    def test_pos_view_payload_carries_unit_symbol(self):
        client = self._client()
        kilogram = self._unit_by_symbol('kg')
        self._create_product(unit_id=kilogram.id)
        payload = client.get('/api/products?view=pos').get_json()
        self.assertTrue(any(
            item['id'] == self.product_ids[-1] and item['unit_symbol'] == 'kg'
            for item in payload['items']
        ))

    def test_product_rejects_unknown_or_inactive_unit(self):
        client = self._client()
        response = client.post('/api/products', json={
            'name': f'Ghost {uuid.uuid4().hex}', 'price': 1.0, 'stock': 1,
            'unit_id': 999999,
        })
        self.assertEqual(response.status_code, 400)

        inactive = self._create_unit(is_active=False)
        response = client.post('/api/products', json={
            'name': f'Ghost {uuid.uuid4().hex}', 'price': 1.0, 'stock': 1,
            'unit_id': inactive.id,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('inactive', response.get_json()['message'])

    # --- Delete guards ---

    def test_delete_unit_used_by_product_is_blocked(self):
        custom = self._create_unit()
        self._create_product(unit_id=custom.id)
        client = self._client()
        response = client.delete(f'/api/units/{custom.id}')
        self.assertEqual(response.status_code, 409)
        self.assertIn('still use it', response.get_json()['message'])
        self.assertIsNotNone(db.session.get(Unit, custom.id))

    def test_delete_base_unit_with_children_is_blocked(self):
        gram = self._unit_by_symbol('g')
        client = self._client()
        response = client.delete(f'/api/units/{gram.id}')
        self.assertEqual(response.status_code, 409)
        self.assertIn('convert from it', response.get_json()['message'])

    def test_delete_unused_unit_succeeds(self):
        custom = self._create_unit()
        client = self._client()
        response = client.delete(f'/api/units/{custom.id}')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(db.session.get(Unit, custom.id))
        self.unit_ids.remove(custom.id)

    def test_update_unit_factor_changes_conversion(self):
        base = self._create_unit()
        child = self._create_unit(
            unit_type='weight', base_unit_id=base.id, factor_to_base=10.0
        )
        client = self._client()
        response = client.put(f'/api/units/{child.id}', json={'factor_to_base': 25.0})
        self.assertEqual(response.status_code, 200)
        response = client.get(
            f'/api/units/convert?from_id={child.id}&to_id={base.id}&quantity=2'
        )
        self.assertEqual(response.get_json()['converted'], 50.0)

    # --- Bug-mission regressions (.scratch/bug-mission-units-2026-09-30.md) ---

    def test_cannot_demote_base_unit_that_has_children(self):
        """Blocker #1: demoting a base would silently break its children's math."""
        base = self._create_unit()
        self._create_unit(unit_type='weight', base_unit_id=base.id, factor_to_base=5.0)
        new_root = self._create_unit(unit_type='weight')
        client = self._client()
        response = client.put(
            f'/api/units/{base.id}',
            json={'base_unit_id': new_root.id, 'factor_to_base': 2.0},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('convert from it', response.get_json()['message'])
        self.assertIsNone(db.session.get(Unit, base.id).base_unit_id)

    def test_chain_conversion_walks_every_hop(self):
        """Blocker #1 math backstop: legacy two-hop chains still convert right."""
        root = self._create_unit()
        mid = self._create_unit(
            unit_type='weight', base_unit_id=root.id, factor_to_base=10.0
        )
        suffix = uuid.uuid4().hex[:6]
        leaf = Unit(
            # Built directly (bypassing validation) to simulate legacy data.
            name=f'Leaf {suffix}', symbol=f'lf{suffix}', unit_type='weight',
            base_unit_id=mid.id, factor_to_base=3.0,
        )
        db.session.add(leaf)
        db.session.commit()
        self.unit_ids.append(leaf.id)
        converted = convert_unit_quantity(leaf, root, 2)
        self.assertEqual(float(converted), 60.0)

    def test_product_update_keeps_inactive_unit_assignment(self):
        """Major #3: editing a product must not wipe a deactivated unit."""
        custom = self._create_unit(is_active=False)
        product = self._create_product(unit_id=custom.id)
        client = self._client()
        response = client.put(
            f'/api/products/{product.id}',
            json={'unit_id': custom.id, 'price': 12.0},
        )
        self.assertEqual(response.status_code, 200)
        single = client.get(f'/api/products/{product.id}').get_json()
        self.assertEqual(single['unit_id'], custom.id)
        self.assertEqual(float(single['price']), 12.0)
        # Assigning the inactive unit to a DIFFERENT product is still refused.
        other = self._create_product()
        response = client.put(
            f'/api/products/{other.id}', json={'unit_id': custom.id}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('inactive', response.get_json()['message'])

    def test_convert_rejects_oversized_quantity(self):
        """Minor #4: quantize() must never surface as an unhandled 500."""
        client = self._client()
        gram = self._unit_by_symbol('g')
        kilogram = self._unit_by_symbol('kg')
        response = client.get(
            f'/api/units/convert?from_id={kilogram.id}&to_id={gram.id}&quantity=1e25'
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('too large', response.get_json()['message'])


if __name__ == '__main__':
    unittest.main()
