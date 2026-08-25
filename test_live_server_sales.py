"""Live-server integration + network-level tests for offline sale sync.

Starts a real Flask dev server on 127.0.0.1:5057 and exercises the actual
HTTP stack: routing, auth, JSON parsing, idempotent replay, stock
validation, concurrent writers, and connection-refused behaviour.
"""

import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid

import requests

from app import app, db, Branch, Product, Sale, User

BASE = "http://127.0.0.1:5057"
_server = None


def _start_server():
    global _server
    _server = subprocess.Popen(
        [sys.executable, "-c",
         "from app import app; app.run(host='127.0.0.1', port=5057, threaded=True)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", 5057), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("Test server did not start")


def _stop_server():
    if _server:
        _server.terminate()
        _server.wait(timeout=10)


class LiveServerSaleTests(unittest.TestCase):
    """API + network level checks against a real HTTP server."""

    @classmethod
    def setUpClass(cls):
        _start_server()
        with app.app_context():
            cls.user_id = User.query.filter_by(username='admin').first().id
            cls.branch_id = Branch.query.filter_by(is_active=True).first().id
            cls.product = Product(
                barcode='LIVE-' + uuid.uuid4().hex[:10],
                name='Live Server Test Product',
                price=1000.0, cost=500.0, stock=50, tax_rate=0.0,
                branch_id=cls.branch_id,
            )
            db.session.add(cls.product)
            db.session.commit()
            cls.product_id = cls.product.id
            # Real login to get a session cookie over actual HTTP
            cls.session = requests.Session()
            resp = cls.session.post(BASE + '/login', data={
                'username': 'admin',
                'password': 'admin123',
            }, allow_redirects=False)
            cls.logged_in = resp.status_code in (200, 302)

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            Sale.query.filter(Sale.transaction_id.like('live-%')).delete()
            product = db.session.get(Product, cls.product_id)
            if product:
                db.session.delete(product)
            db.session.commit()
        _stop_server()

    def _payload(self, txn=None, quantity=1, product_id=None):
        payload = {
            'items': [{
                'product_id': product_id or self.product_id,
                'price': 1000.0, 'quantity': quantity, 'tax_rate': 0,
            }],
            'payment_method': 'cash',
            'cash_received': 1000.0 * quantity,
        }
        if txn:
            payload['transaction_id'] = txn
        return payload

    # ------------------------------------------------------ API level

    def test_01_unauthenticated_request_rejected_over_http(self):
        resp = requests.post(BASE + '/api/sales', json=self._payload('live-anon'))
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(resp.headers.get('Content-Type', '').startswith('application/json'))

    def test_02_malformed_json_rejected(self):
        resp = self.session.post(
            BASE + '/api/sales', data='{not valid json',
            headers={'Content-Type': 'application/json'})
        self.assertIn(resp.status_code, (400, 500))

    def test_03_missing_items_rejected(self):
        resp = self.session.post(BASE + '/api/sales', json={'payment_method': 'cash'})
        self.assertEqual(resp.status_code, 400)

    def test_04_create_then_replay_over_real_http(self):
        txn = 'live-' + uuid.uuid4().hex
        first = self.session.post(BASE + '/api/sales', json=self._payload(txn))
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()['transaction_id'], txn)

        replay = self.session.post(BASE + '/api/sales', json=self._payload(txn))
        self.assertEqual(replay.status_code, 200)
        body = replay.json()
        self.assertTrue(body['success'])
        self.assertTrue(body['duplicate'])
        self.assertEqual(body['transaction_id'], txn)
        self.assertIn('created_at', body)

        with app.app_context():
            self.assertEqual(Sale.query.filter_by(transaction_id=txn).count(), 1)

    def test_05_replay_does_not_double_decrement_stock(self):
        txn = 'live-' + uuid.uuid4().hex
        self.session.post(BASE + '/api/sales', json=self._payload(txn, quantity=3))
        with app.app_context():
            before = db.session.get(Product, self.product_id).stock
        self.session.post(BASE + '/api/sales', json=self._payload(txn, quantity=3))
        with app.app_context():
            after = db.session.get(Product, self.product_id).stock
        self.assertEqual(before, after)

    def test_06_insufficient_stock_rejected(self):
        txn = 'live-' + uuid.uuid4().hex
        resp = self.session.post(BASE + '/api/sales', json=self._payload(txn, quantity=9999))
        self.assertEqual(resp.status_code, 400)
        with app.app_context():
            self.assertEqual(Sale.query.filter_by(transaction_id=txn).count(), 0)

    # ------------------------------------------------ network level

    def test_07_concurrent_replays_create_exactly_one_sale(self):
        """Two simultaneous retries of the same offline sale -> one row."""
        txn = 'live-' + uuid.uuid4().hex
        results = []

        def post():
            results.append(self.session.post(BASE + '/api/sales', json=self._payload(txn)))

        threads = [threading.Thread(target=post) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        statuses = sorted(r.status_code for r in results)
        self.assertIn(statuses[0], (200, 201))
        self.assertIn(statuses[1], (200, 201))
        with app.app_context():
            self.assertEqual(Sale.query.filter_by(transaction_id=txn).count(), 1)

    def test_08_connection_refused_behaves_like_offline(self):
        """A closed port must fail fast with ConnectionError (client queues)."""
        with self.assertRaises(requests.exceptions.ConnectionError):
            requests.post('http://127.0.0.1:5058/api/sales',
                          json=self._payload('live-x'), timeout=3)

    def test_09_health_endpoint_responds(self):
        resp = self.session.get(BASE + '/healthz')
        self.assertEqual(resp.status_code, 200)


if __name__ == '__main__':
    unittest.main()
