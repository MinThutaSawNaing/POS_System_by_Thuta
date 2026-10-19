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
                            branch_id=b.id, batch_number='OLD')])
        m.db.session.commit()
        self.po, self.line, self.debt = po.id, line.id, debt_b.id
        self.tools = AITools(m.db, m.AI_MODELS)
        self.tools.set_context(dict(user_id=self.user, role='manager', branch_id=self.a))

    def tearDown(self):
        self.module.db.session.remove()
        self.context.pop()

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
        result = t.receive_purchase_order(self.po)
        self.assertTrue(result['success'], result)
        received = m.WarehouseInventory.query.filter_by(batch_number='B-PO').one()
        self.assertEqual((received.branch_id, received.product_id, received.quantity), (self.b, self.foreign, 5))
        self.assertEqual(m.db.session.get(m.Product, self.foreign).stock, 8)
        result = t.create_purchase_order(self.supplier, [{'product_id': self.foreign, 'quantity': 2}])
        self.assertTrue(result['success'], result)
        self.assertTrue(t.cancel_purchase_order(result['po_id'], 'Not needed')['success'])

    def test_receiving_validates_entire_payload_and_counts_order_once(self):
        t, m = self.tools, self.module
        t.approve_purchase_order(self.po)
        result = t.receive_purchase_order(self.po, [
            {'purchase_order_item_id': self.line, 'received_qty': 2},
            {'purchase_order_item_id': self.line, 'received_qty': 4}])
        self.assertIn('error', result)
        self.assertEqual(m.db.session.get(m.PurchaseOrderItem, self.line).received_qty, 0)
        self.assertEqual(m.WarehouseInventory.query.count(), 1)
        for quantity in (2, 3):
            self.assertTrue(t.receive_purchase_order(self.po, [
                {'purchase_order_item_id': self.line, 'received_qty': quantity}])['success'])
        self.assertEqual(m.db.session.get(m.Supplier, self.supplier).total_orders, 1)
        self.assertIn('error', t.receive_purchase_order(self.po))

    def test_warehouse_shared_transfer_defaults_to_trusted_branch(self):
        t, m = self.tools, self.module
        self.assertEqual(t.get_warehouse_inventory()['warehouse_items'][0]['product_id'], self.foreign)
        result = t.create_warehouse_transfer(self.foreign, 3)
        self.assertTrue(result['success'], result)
        target = m.db.session.get(m.Product, result['target_product_id'])
        self.assertEqual((target.branch_id, target.barcode, target.stock), (self.a, 'PHYSICAL', 3))
        self.assertEqual(target.category_ref.branch_id, self.a)
        self.assertEqual(m.db.session.get(m.Product, self.foreign).stock, 8)
        self.assertEqual(m.WarehouseInventory.query.one().quantity, 7)
        self.assertEqual(t.get_warehouse_transfer_history()['transfers'][0]['target_branch_id'], self.a)
        result = t.create_warehouse_transfer(self.foreign, 2, target_branch_id=self.b)
        self.assertTrue(result['success'], result)
        self.assertEqual(m.db.session.get(m.Product, self.foreign).stock, 10)

    def test_warehouse_never_merges_unrelated_same_name_and_rolls_back_failure(self):
        t, m = self.tools, self.module
        m.db.session.get(m.Product, self.local).name = 'Other'
        m.db.session.commit()
        with patch.object(m.db.session, 'commit', side_effect=RuntimeError('injected')):
            self.assertIn('error', t.create_warehouse_transfer(self.foreign, 3))
        self.assertEqual(m.WarehouseInventory.query.one().quantity, 10)
        self.assertEqual(m.Product.query.count(), 2)
        result = t.create_warehouse_transfer(self.foreign, 3)
        self.assertTrue(result['success'], result)
        self.assertNotEqual(result['target_product_id'], self.local)
        self.assertEqual(m.db.session.get(m.Product, self.local).stock, 4)

    def test_transfer_rejects_catalog_barcode_drift_without_mutations(self):
        t, m = self.tools, self.module
        result = t.create_warehouse_transfer(self.foreign, 3)
        self.assertTrue(result['success'], result)
        target_id = result['target_product_id']
        for barcode in ('NEW', None):
            with self.subTest(destination_barcode=barcode):
                target = m.db.session.get(m.Product, target_id)
                target.barcode = barcode
                m.db.session.commit()
                result = t.create_warehouse_transfer(self.foreign, 2)
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
                self.assertIn('error', self.tools.create_warehouse_transfer(self.foreign, quantity))
        self.assertEqual(self.module.WarehouseInventory.query.one().quantity, 10)

    def test_concurrent_shared_transfers_cannot_oversell(self):
        m = self.module

        def transfer(_):
            with self.app.app_context():
                tools = AITools(m.db, m.AI_MODELS)
                tools.set_context(dict(user_id=self.user, role='manager', branch_id=self.a))
                try:
                    return tools.create_warehouse_transfer(self.foreign, 7)
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

    def test_cashier_cannot_access_shared_tools_even_with_forged_context(self):
        names = ('get_supplier_list', 'get_supplier_details', 'get_supplier_categories',
                 'get_purchase_order_products', 'get_purchase_orders', 'create_supplier',
                 'update_supplier', 'delete_supplier', 'create_purchase_order', 'approve_purchase_order',
                 'cancel_purchase_order', 'receive_purchase_order', 'get_warehouse_inventory',
                 'create_warehouse_transfer', 'get_warehouse_transfer_history', 'get_supplier_price_for_product')
        with self.app.test_request_context('/'):
            session.update(user_id=self.user, role='cashier', branch_id=self.a)
            for name in names:
                with self.subTest(tool=name):
                    self.assertEqual(TOOL_METADATA[name]['requires_role'], 'manager')
                    self.assertIn('manager', getattr(self.tools, name)()['error'])
            self.assertEqual(self.tools.get_product_details(product_id=self.foreign)['error'], 'Product not found')


if __name__ == '__main__':
    unittest.main()