import json
import unittest
import uuid
from datetime import datetime

from app import (AuditLog, Branch, Product, User, app, audit_day_utc_bounds,
                 audit_local_datetime, db)


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        with app.app_context():
            self.branch = Branch.query.filter_by(is_active=True).first()
            self.manager = User.query.filter_by(role='manager').first()
            self.cashier = User.query.filter_by(role='cashier').first()
            if not self.cashier:
                self.cashier = User(username=f'audit_cashier_{uuid.uuid4().hex[:8]}',
                                    password='unused', role='cashier')
                db.session.add(self.cashier)
                db.session.commit()
            self.branch_id = self.branch.id
            self.manager_id = self.manager.id
            self.cashier_id = self.cashier.id
            self.created_product_ids = []
            self.created_log_ids = []
            self.created_user_ids = []

    def tearDown(self):
        with app.app_context():
            # Audit rows are intentionally append-only through the ORM. Test
            # fixtures use SQL cleanup so production code cannot gain a delete seam.
            if self.created_log_ids:
                db.session.info['_allow_audit_log_maintenance'] = True
                try:
                    db.session.execute(
                        AuditLog.__table__.delete().where(AuditLog.id.in_(self.created_log_ids)))
                finally:
                    db.session.info.pop('_allow_audit_log_maintenance', None)
            if self.created_product_ids:
                db.session.execute(
                    Product.__table__.delete().where(Product.id.in_(self.created_product_ids)))
            if self.created_user_ids:
                db.session.execute(
                    User.__table__.delete().where(User.id.in_(self.created_user_ids)))
            db.session.commit()

    def client_for(self, user_id, role, username):
        client = app.test_client()
        with client.session_transaction() as current:
            current['user_id'] = user_id
            current['role'] = role
            current['username'] = username
            current['branch_id'] = self.branch_id
        return client

    def test_product_create_is_logged_with_actor_and_redacted_request_context(self):
        client = self.client_for(self.manager_id, 'manager', self.manager.username)
        name = f'Audit Product {uuid.uuid4().hex[:8]}'
        response = client.post('/api/products', json={
            'name': name, 'barcode': uuid.uuid4().hex[:12], 'price': '12.50',
            'cost': '7.00', 'stock': '4', 'tax_rate': '0',
        })
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        with app.app_context():
            product_id = Product.query.filter_by(name=name, branch_id=self.branch_id).one().id
            self.created_product_ids.append(product_id)
            row = AuditLog.query.filter_by(entity_type='Product', entity_id=str(product_id),
                                           action='create').order_by(AuditLog.id.desc()).first()
            self.assertIsNotNone(row)
            self.created_log_ids.append(row.id)
            self.assertEqual(row.actor_user_id, self.manager_id)
            self.assertEqual(row.actor_username, self.manager.username)
            self.assertEqual(row.branch_id, self.branch_id)
            self.assertEqual(row.category, 'Products')
            self.assertEqual(row.request_path, '/api/products')
            self.assertIn(name, row.summary)

    def test_sale_records_financial_event_and_stock_change(self):
        with app.app_context():
            previous_max_log_id = db.session.query(db.func.max(AuditLog.id)).scalar() or 0
            product = Product(name=f'Audit Sale Product {uuid.uuid4().hex[:8]}',
                              barcode=uuid.uuid4().hex[:12], price=10, cost=4,
                              stock=5, tax_rate=0, branch_id=self.branch_id)
            db.session.add(product)
            db.session.commit()
            product_id = product.id
            self.created_product_ids.append(product_id)

        client = self.client_for(self.manager_id, 'manager', self.manager.username)
        response = client.post('/api/sales', json={
            'transaction_id': f'AUDIT-{uuid.uuid4()}',
            'items': [{'product_id': product_id, 'price': 10, 'quantity': 2}],
            'payment_method': 'cash', 'cash_received': 20,
        })
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        transaction_id = response.get_json()['transaction_id']

        with app.app_context():
            sale_log = AuditLog.query.filter_by(entity_type='Sale',
                                                entity_label=transaction_id,
                                                action='create').first()
            stock_log = AuditLog.query.filter_by(entity_type='Product',
                                                 entity_id=str(product_id),
                                                 action='update').order_by(AuditLog.id.desc()).first()
            self.assertIsNotNone(sale_log)
            self.assertEqual(sale_log.category, 'Sales')
            self.assertIsNotNone(stock_log)
            stock_change = json.loads(stock_log.changes_json)['stock']
            self.assertEqual(stock_change, {'before': 5, 'after': 3})
            self.created_log_ids.extend([sale_log.id, stock_log.id])
            # Child SaleItem logs are expected too; include all transaction-created
            # rows in fixture cleanup while retaining them for the assertions above.
            extra_logs = AuditLog.query.filter(
                AuditLog.request_path == '/api/sales',
                AuditLog.id > previous_max_log_id,
                AuditLog.id.notin_(self.created_log_ids),
            ).all()
            self.created_log_ids.extend(row.id for row in extra_logs)
            sale_id = sale_log.entity_id
            db.session.execute(db.text('DELETE FROM sale_item WHERE sale_id = :sale_id'),
                               {'sale_id': sale_id})
            db.session.execute(db.text('DELETE FROM sale WHERE id = :sale_id'),
                               {'sale_id': sale_id})
            db.session.commit()

    def test_user_password_is_never_stored_in_audit_details(self):
        client = self.client_for(self.manager_id, 'manager', self.manager.username)
        username = f'audit_user_{uuid.uuid4().hex[:8]}'
        secret = 'NeverStoreThisPassword!'
        response = client.post('/api/users', json={
            'username': username, 'password': secret, 'role': 'cashier'
        })
        self.assertEqual(response.status_code, 201)
        with app.app_context():
            user = User.query.filter_by(username=username).first()
            self.created_user_ids.append(user.id)
            row = AuditLog.query.filter_by(entity_type='User', entity_id=str(user.id),
                                           action='create').order_by(AuditLog.id.desc()).first()
            self.assertIsNotNone(row)
            self.created_log_ids.append(row.id)
            self.assertNotIn(secret, row.changes_json)
            self.assertEqual(json.loads(row.changes_json)['after']['password'], '[REDACTED]')

    def test_rollback_does_not_leave_a_false_log(self):
        marker = f'Rollback Product {uuid.uuid4().hex}'
        with app.test_request_context('/api/products', method='POST'):
            from flask import session
            session.update(user_id=self.manager_id, username=self.manager.username,
                           role='manager', branch_id=self.branch_id)
            product = Product(name=marker, barcode=uuid.uuid4().hex, price=1, cost=1,
                              stock=1, tax_rate=0, branch_id=self.branch_id)
            db.session.add(product)
            db.session.flush()
            db.session.rollback()
        with app.app_context():
            self.assertIsNone(AuditLog.query.filter(AuditLog.summary.contains(marker)).first())

    def test_logs_endpoint_is_protected_filterable_and_paginated(self):
        cashier = self.client_for(self.cashier_id, 'cashier', self.cashier.username)
        self.assertEqual(cashier.get('/api/logs').status_code, 403)

        manager = self.client_for(self.manager_id, 'manager', self.manager.username)
        response = manager.get('/api/logs?category=Products&action=create&per_page=1')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn('items', payload)
        self.assertLessEqual(len(payload['items']), 1)
        self.assertIn('summary', payload)
        self.assertIn('categories', payload)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store, max-age=0')

    def test_invalid_log_filters_fail_closed(self):
        manager = self.client_for(self.manager_id, 'manager', self.manager.username)
        for path in (
            '/api/logs?start=not-a-date',
            '/api/logs?start=2026-10-02&end=2026-10-01',
            '/api/logs?action=destroy',
            '/api/logs?branch_id=not-a-number',
            '/api/logs/export.txt?end=2026-99-99',
        ):
            response = manager.get(path)
            self.assertEqual(response.status_code, 400, path)

    def test_bulk_orm_update_leaves_audit_evidence(self):
        with app.app_context():
            product = Product(name=f'Bulk Audit {uuid.uuid4().hex[:8]}',
                              barcode=uuid.uuid4().hex[:12], price=1, cost=1,
                              stock=3, tax_rate=0, branch_id=self.branch_id)
            db.session.add(product)
            db.session.commit()
            self.created_product_ids.append(product.id)

        with app.test_request_context('/api/categories/bulk-update', method='POST'):
            from flask import session
            session.update(user_id=self.manager_id, username=self.manager.username,
                           role='manager', branch_id=self.branch_id)
            Product.query.filter_by(id=product.id).update({'stock': 4})
            db.session.commit()

        with app.app_context():
            row = AuditLog.query.filter_by(
                entity_type='Product', action='update',
                request_path='/api/categories/bulk-update',
            ).order_by(AuditLog.id.desc()).first()
            self.assertIsNotNone(row)
            self.assertIn('Bulk update affected 1', row.summary)
            self.created_log_ids.append(row.id)

    def test_daily_grouping_uses_yangon_calendar_date(self):
        # 17:45 UTC is 00:15 on the following day in Myanmar.
        utc_moment = datetime(2026, 9, 30, 17, 45)
        local = audit_local_datetime(utc_moment)
        self.assertEqual(local.strftime('%Y-%m-%d %H:%M:%S'), '2026-10-01 00:15:00')
        start, end = audit_day_utc_bounds('2026-10-01')
        self.assertEqual(start, datetime(2026, 9, 30, 17, 30))
        self.assertEqual(end, datetime(2026, 10, 1, 17, 30))

        with app.app_context():
            row = AuditLog(
                created_at=utc_moment, actor_user_id=self.manager_id,
                actor_username=self.manager.username, actor_role='manager',
                branch_id=self.branch_id, category='Sales', action='create',
                entity_type='Sale', entity_id='daily-test',
                entity_label='DAILY-TEST', summary='Created sale “DAILY-TEST”',
                changes_json='{}', request_method='POST', request_path='/api/sales',
            )
            db.session.add(row)
            db.session.commit()
            self.created_log_ids.append(row.id)

        client = self.client_for(self.manager_id, 'manager', self.manager.username)
        included = client.get('/api/logs?start=2026-10-01&end=2026-10-01&q=DAILY-TEST')
        self.assertEqual(included.status_code, 200)
        item = included.get_json()['items'][0]
        self.assertEqual(item['local_date'], '2026-10-01')
        self.assertEqual(item['local_time'], '00:15:00')
        self.assertEqual(item['timezone'], 'Asia/Yangon')
        excluded = client.get('/api/logs?start=2026-09-30&end=2026-09-30&q=DAILY-TEST')
        self.assertEqual(excluded.get_json()['items'], [])

    def test_txt_export_is_protected_grouped_and_honors_filters(self):
        with app.app_context():
            matching = AuditLog(
                created_at=datetime(2026, 9, 30, 17, 45),
                actor_user_id=self.manager_id, actor_username='မြန်မာ-manager',
                actor_role='manager', branch_id=self.branch_id,
                category='Sales', action='update', entity_type='Sale',
                entity_id='txt-match', entity_label='TXT-MATCH',
                summary='Updated sale TXT-MATCH',
                changes_json=json.dumps({'total': {'before': 10, 'after': 12}}),
                request_method='PUT', request_path='/api/sales/TXT-MATCH',
                ip_address='127.0.0.1',
            )
            other = AuditLog(
                created_at=datetime(2026, 9, 30, 12, 0),
                actor_username='other', category='Products', action='create',
                entity_type='Product', entity_id='txt-other',
                summary='Created product TXT-OTHER', changes_json='{}',
            )
            db.session.add_all([matching, other])
            db.session.commit()
            self.created_log_ids.extend([matching.id, other.id])

        cashier = self.client_for(self.cashier_id, 'cashier', self.cashier.username)
        self.assertEqual(cashier.get('/api/logs/export.txt').status_code, 403)

        manager = self.client_for(self.manager_id, 'manager', self.manager.username)
        response = manager.get(
            '/api/logs/export.txt?category=Sales&start=2026-10-01&end=2026-10-01&q=TXT-MATCH')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith('text/plain'))
        self.assertIn('attachment; filename="parrot_pos_logs_',
                      response.headers['Content-Disposition'])
        text = response.get_data(as_text=True)
        self.assertTrue(text.startswith('\ufeffPARROT POS'))
        self.assertIn('Thursday, 01 October 2026 (2026-10-01)', text)
        self.assertIn('[00:15:00] UPDATE · Sales', text)
        self.assertIn('မြန်မာ-manager', text)
        self.assertIn('"before": 10', text)
        self.assertIn('"after": 12', text)
        self.assertNotIn('TXT-OTHER', text)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store, max-age=0')

    def test_txt_export_flattens_newlines_in_human_readable_fields(self):
        with app.app_context():
            row = AuditLog(
                actor_username='manager\n## FORGED DAY', category='Sales',
                action='create', entity_type='Sale', entity_id='newline-test',
                summary='Real event\n[00:00:00] DELETE · Users', changes_json='{}',
                created_at=datetime(2026, 9, 30, 17, 45),
            )
            db.session.add(row)
            db.session.commit()
            self.created_log_ids.append(row.id)
        client = self.client_for(self.manager_id, 'manager', self.manager.username)
        text = client.get('/api/logs/export.txt?q=newline-test').get_data(as_text=True)
        self.assertNotIn('\n## FORGED DAY', text)
        self.assertNotIn('\n[00:00:00] DELETE · Users', text)

    def test_existing_log_cannot_be_modified_or_deleted_through_orm(self):
        with app.app_context():
            row = AuditLog(actor_username='test', category='System', action='create',
                           entity_type='Fixture', summary='fixture')
            db.session.add(row)
            db.session.commit()
            self.created_log_ids.append(row.id)
            row.summary = 'tampered'
            with self.assertRaises(ValueError):
                db.session.commit()
            db.session.rollback()
            with self.assertRaises(ValueError):
                AuditLog.query.filter_by(id=row.id).delete()
            db.session.rollback()
            db.session.delete(row)
            with self.assertRaises(ValueError):
                db.session.commit()
            db.session.rollback()


if __name__ == '__main__':
    unittest.main()