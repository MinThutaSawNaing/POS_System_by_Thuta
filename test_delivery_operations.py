"""Delivery enterprise backend regressions; every database/upload lives in a disposable root.

Run only this module: python run_tests.py test_delivery_operations.py -q --tb=short
"""
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from flask import Flask


class DeliveryOperationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='pos-delivery-tests-')
        cls.root = Path(cls.temporary.name)
        (cls.root / 'instance').mkdir()
        shutil.copytree(Path(__file__).parent / 'templates', cls.root / 'templates')
        original_init = Flask.__init__

        def isolated_init(app, *args, **kwargs):
            kwargs.update(instance_path=str(cls.root / 'instance'), root_path=str(cls.root))
            original_init(app, *args, **kwargs)

        name = '_isolated_delivery_operations_app'
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

    def delivery(self, stage='delivering', branch=None, **fields):
        m = self.module
        from datetime import datetime, timedelta
        import uuid
        with self.app.app_context():
            sale = m.Sale(transaction_id=uuid.uuid4().hex, total=100, tax=0, user_id=self.user, branch_id=branch or self.a)
            m.db.session.add(sale)
            m.db.session.flush()
            d = m.Delivery(delivery_number=uuid.uuid4().hex, sale_id=sale.id, stage=stage, branch_id=branch or self.a,
                           payment_status='cash_on_delivery', delivery_fee=5, created_at=datetime.utcnow()-timedelta(hours=49), **fields)
            m.db.session.add(d)
            m.db.session.commit()
            return d.id

    def post(self, id, action, **data):
        return self.client().post(f'/api/deliveries/{id}/{action}', json=data)

    def test_attempt_retry_and_history(self):
        id = self.delivery()
        r = self.post(id, 'attempts', outcome='failed', reason='Not home')
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json['delivery']['stage'], 'delivering')
        self.assertTrue(r.json['delivery']['has_failed_attempt'])
        self.assertEqual(self.post(id, 'retry', note='Tomorrow', scheduled_at='2026-11-01T10:00:00+06:30').status_code, 200)
        detail = self.client().get(f'/api/deliveries/{id}').json
        self.assertEqual(detail['stage'], 'packaged')
        self.assertEqual(detail['scheduled_at'], '2026-11-01T03:30:00')
        self.assertFalse(detail['has_failed_attempt'])
        self.assertEqual(self.post(id, 'retry').status_code, 400)
        history = self.client().get(f'/api/deliveries/{id}/history').json['items']
        self.assertEqual([h['action'] for h in history if h['action'] in ('retry', 'attempt')], ['retry', 'attempt'])
        self.assertEqual(next(h for h in history if h['action'] == 'retry')['actor_name'], 'manager')

    def test_collection_remittance_idempotency_and_sale_unchanged(self):
        id = self.delivery()
        self.assertEqual(self.post(id, 'collection', amount='105.00', reference='C1').status_code, 400)
        self.assertEqual(self.post(id, 'attempts', outcome='delivered', proof_note='Signed').status_code, 200)
        for bad in ['104.99', '105.001', 'NaN', '-1', True]:
            self.assertEqual(self.post(id, 'collection', amount=bad, reference='C1').status_code, 400)
        for _ in range(2):
            r = self.post(id, 'collection', amount='105.00', reference='C1')
            self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(self.post(id, 'collection', amount='105.00', reference='different').status_code, 409)
        same = self.client().put(f'/api/deliveries/{id}',json={'payment_status':'cash_on_delivery','delivery_fee':5,'courier_name':'Other'})
        self.assertEqual(same.status_code,200,same.json)
        self.assertEqual(self.client().put(f'/api/deliveries/{id}', json={'payment_status':'paid'}).status_code, 400)
        for _ in range(2):
            r = self.post(id, 'remittance', amount='105.00', reference='R1')
            self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json['delivery']['cod'], {'status':'remitted','expected_amount':'105.00','collected_amount':'105.00','remitted_amount':'105.00'})
        m = self.module
        with self.app.app_context():
            self.assertEqual(m.db.session.get(m.Delivery,id).sale.total,100)
            self.assertEqual(m.DeliveryEvent.query.filter_by(action='collection').count(),1)

    def test_list_stats_filters_and_bulk_atomic(self):
        first = self.delivery()
        second = self.delivery('delivered')
        self.assertIsInstance(self.client().get('/api/deliveries').json,list)
        result = self.client().get('/api/deliveries?page=1&per_page=1').json
        self.assertEqual((result['total'],result['pages'],len(result['items'])),(2,2,1))
        self.assertEqual(len(self.client().get('/api/deliveries?attention=overdue').json),1)
        self.assertEqual(self.client().get('/api/deliveries?page=0').status_code,400)
        r = self.client().post('/api/deliveries/bulk-assign',json={'delivery_ids':[first,second],'courier_name':'Test'})
        self.assertEqual(r.status_code,400)
        self.assertIsNone(self.client().get(f'/api/deliveries/{first}').json['courier_name'])
        r = self.client().post('/api/deliveries/bulk-assign',json={'delivery_ids':[first],'courier_name':'Express','courier_phone':'123'})
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(len(self.client().get('/api/deliveries?courier=PRESS').json),1)
        self.assertEqual(self.client().get('/api/deliveries?q=notpresent&page=1').json['total'],0)
        stats = self.client().get('/api/deliveries/stats').json
        self.assertEqual(stats['overdue_open'],1)
        self.assertEqual(stats['unassigned_open'],0)
        self.assertEqual(stats['cod_outstanding'],'210.00')

    def test_permissions_scoping_and_stale_identity(self):
        id = self.delivery()
        other = self.delivery(branch=self.b)
        self.assertEqual(self.post(other,'attempts',outcome='failed',reason='x').status_code,404)
        self.assertEqual(self.client().get(f'/api/deliveries/{other}/history').status_code,404)
        self.assertEqual(self.client(role='cashier').post(f'/api/deliveries/{id}/attempts',json={'outcome':'failed','reason':'x'}).status_code,403)
        r = self.client().post(f'/api/deliveries/{id}/attempts',json={'outcome':'failed','reason':'x'},headers={'X-POS-User-ID':'999','X-POS-Role':'manager'})
        self.assertEqual(r.status_code,409)
        self.assertEqual(self.client().get(f'/api/deliveries/{id}').json['attempt_count'],0)

    def test_concurrent_collection_is_single_ledger_entry(self):
        id = self.delivery('delivered')
        def collect(_):
            return self.post(id, 'collection', amount='105.00', reference='same').status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(collect, range(2)))
        self.assertEqual(codes, [200, 200])
        with self.app.app_context():
            self.assertEqual(self.module.DeliveryEvent.query.filter_by(action='collection').count(), 1)

    def test_invalid_dates_failed_filter_and_audit_existing_update(self):
        id = self.delivery()
        for invalid in [123, [], {}, 'bad']:
            self.assertEqual(self.post(id, 'retry', scheduled_at=invalid).status_code,400)
            self.assertEqual(self.client().put(f'/api/deliveries/{id}',json={'scheduled_at':invalid}).status_code,400)
        self.post(id,'attempts',outcome='failed',reason='x')
        self.assertEqual(self.client().get('/api/deliveries?attention=failed&page=1').json['total'],1)
        self.post(id,'retry')
        self.assertEqual(self.client().get('/api/deliveries?attention=failed&page=1').json['total'],0)
        r = self.client().put(f'/api/deliveries/{id}',json={'courier_name':'New'})
        self.assertEqual(r.status_code,200,r.json)
        history = self.client().get(f'/api/deliveries/{id}/history').json['items']
        self.assertTrue(any(h['action']=='updated' and 'courier_name' in h['note'] and h['actor_name']=='manager' for h in history))

    def test_orm_normalizes_offsets_and_rejects_stale_collected_cancellation(self):
        from datetime import datetime
        m = self.module
        id = self.delivery('delivered')
        with self.app.app_context():
            d = m.db.session.get(m.Delivery, id)
            d.scheduled_at = datetime.fromisoformat('2026-11-05T10:00:00+06:30')
            m.db.session.commit()
            self.assertEqual(d.scheduled_at, datetime(2026,11,5,3,30))
        # Load an independent stale identity map and its empty event collection.
        with self.app.app_context():
            stale = type(m.db.session())(m.db, bind=m.db.engine)
            d = stale.get(m.Delivery, id)
            list(d.events)
            self.assertEqual(self.post(id,'collection',amount='105.00',reference='C').status_code,200)
            d.stage = 'cancelled'
            with self.assertRaisesRegex(ValueError,'remain delivered'):
                stale.commit()
            stale.rollback()
            stale.close()
            m.db.session.expire_all()
            self.assertEqual(m.db.session.get(m.Delivery,id).stage,'delivered')

    def test_cod_rejections_do_not_write_events_or_sale(self):
        id = self.delivery('delivered')
        m = self.module
        with self.app.app_context():
            baseline = m.DeliveryEvent.query.count()
        self.assertEqual(self.post(id,'remittance',amount='105.00',reference='R').status_code,400)
        paid = self.delivery('delivered')
        with self.app.app_context():
            d = m.db.session.get(m.Delivery,paid)
            d.payment_status = 'paid'
            m.db.session.commit()
            baseline = m.DeliveryEvent.query.count()
        self.assertEqual(self.post(paid,'collection',amount='105.00',reference='P').status_code,400)
        self.assertEqual(self.post(paid,'remittance',amount='105.00',reference='P').status_code,400)
        with self.app.app_context():
            self.assertEqual(m.DeliveryEvent.query.count(),baseline)
        self.assertEqual(self.post(id,'collection',amount='105.00',reference='C').status_code,200)
        self.assertEqual(self.post(id,'remittance',amount='104.99',reference='R').status_code,400)
        self.assertEqual(self.post(id,'remittance',amount='105.00',reference='R').status_code,200)
        self.assertEqual(self.post(id,'remittance',amount='105.00',reference='different').status_code,409)
        with self.app.app_context():
            self.assertEqual(m.db.session.get(m.Delivery,id).sale.total,100)
            self.assertEqual(m.DeliveryEvent.query.filter_by(action='remittance').count(),1)
