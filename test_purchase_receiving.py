"""Shared receiving is atomic across branches and concurrent manager requests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import importlib.util
import shutil
import sys
import tempfile
from unittest.mock import patch
import unittest
from flask import Flask


class PurchaseReceivingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='pos-receiving-')
        root = Path(cls.temporary.name)
        shutil.copytree(Path(__file__).parent / 'templates', root / 'templates')
        original = Flask.__init__

        def isolated_init(app, *args, **kwargs):
            kwargs.update(root_path=str(root), instance_path=str(root / 'instance'))
            original(app, *args, **kwargs)

        spec = importlib.util.spec_from_file_location('_receiving_test_app', Path(__file__).with_name('app.py'))
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
        cls.temporary.cleanup()

    def setUp(self):
        m = self.m
        with m.app.app_context():
            m.db.drop_all()
            m.db.create_all()
            local = m.Branch(name='Local', code='L', is_default=True)
            foreign = m.Branch(name='Catalog', code='F')
            m.db.session.add_all([local, foreign])
            m.db.session.flush()
            user = m.User(username='manager', password='test', role='manager', branch_id=local.id)
            product = m.Product(name='Other branch product', barcode='RECEIVE', price=10, stock=0, branch_id=foreign.id)
            supplier = m.Supplier(name='Shared', branch_id=foreign.id)
            m.db.session.add_all([user, product, supplier])
            m.db.session.flush()
            po = m.PurchaseOrder(po_number='PO-TEST', supplier_id=supplier.id, status='approved',
                                 total_amount=30, created_by=user.id, branch_id=local.id)
            m.db.session.add(po)
            m.db.session.flush()
            item = m.PurchaseOrderItem(purchase_order_id=po.id, product_id=product.id,
                                      ordered_qty=10, received_qty=0, unit_cost=3)
            m.db.session.add(item)
            m.db.session.commit()
            self.user, self.local, self.foreign = user.id, local.id, foreign.id
            self.po, self.item, self.product, self.supplier = po.id, item.id, product.id, supplier.id

    def receive(self, quantity, role='manager', **changes):
        client = self.m.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=self.user, role=role, branch_id=self.local)
        data = {'items': [{'purchase_order_item_id': self.item, 'received_qty': quantity}]}
        data.update(changes)
        return client.post(f'/api/purchase_orders/{self.po}/receive', json=data)

    def test_partial_receipts_share_product_branch_and_count_order_once(self):
        self.assertEqual(self.receive(4).status_code, 200)
        self.assertEqual(self.receive(6).status_code, 200)
        self.assertEqual(self.receive(1).status_code, 400)
        with self.m.app.app_context():
            rows = self.m.WarehouseInventory.query.all()
            self.assertEqual(sum(row.quantity for row in rows), 10)
            self.assertTrue(all(row.branch_id == self.foreign for row in rows))
            self.assertEqual(self.m.db.session.get(self.m.Supplier, self.supplier).total_orders, 1)

    def test_invalid_and_unapproved_receipts_never_mutate(self):
        for quantity in (True, -1, 1.5, 'bad', 0, 11):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.receive(quantity).status_code, 400)
        self.assertEqual(self.receive(1, items=[
            {'purchase_order_item_id': self.item, 'received_qty': 6},
            {'purchase_order_item_id': self.item, 'received_qty': 6},
        ]).status_code, 400)
        with self.m.app.app_context():
            self.assertEqual(self.m.WarehouseInventory.query.count(), 0)
            po = self.m.db.session.get(self.m.PurchaseOrder, self.po)
            po.status = 'draft'
            self.m.db.session.commit()
        self.assertEqual(self.receive(1).status_code, 400)
        self.assertEqual(self.receive(1, role='cashier').status_code, 403)

    def test_concurrent_receipts_cannot_double_receive(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: self.receive(6).status_code, range(2)))
        self.assertEqual(sorted(statuses), [200, 400])
        with self.m.app.app_context():
            self.assertEqual(self.m.db.session.get(self.m.PurchaseOrderItem, self.item).received_qty, 6)
            self.assertEqual(sum(row.quantity for row in self.m.WarehouseInventory.query.all()), 6)

    def test_shared_product_photo_survives_other_branch_update_and_delete(self):
        m = self.m
        with m.app.app_context():
            first = m.db.session.get(m.Product, self.product)
            first.photo_filename = 'shared.png'
            second = m.Product(name='Local photo clone', price=10, stock=0,
                               photo_filename='shared.png', branch_id=self.local)
            m.db.session.add(second)
            m.db.session.commit()
            second_id = second.id
            photo = Path(m.app.config['UPLOAD_FOLDER']) / 'shared.png'
            photo.write_bytes(b'untouched shared image')
        client = m.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=self.user, role='manager', branch_id=self.local)
        self.assertEqual(client.put(f'/api/products/{second_id}', json={'remove_photo': True}).status_code, 200)
        self.assertTrue(photo.exists())
        with m.app.app_context():
            second = m.db.session.get(m.Product, second_id)
            second.photo_filename = 'shared.png'
            m.db.session.commit()
        self.assertEqual(client.delete(f'/api/products/{second_id}').status_code, 200)
        self.assertEqual(photo.read_bytes(), b'untouched shared image')
        with client.session_transaction() as session:
            session['branch_id'] = self.foreign
        self.assertEqual(client.put(f'/api/products/{self.product}', json={'remove_photo': True}).status_code, 200)
        self.assertFalse(photo.exists())


if __name__ == '__main__':
    unittest.main()