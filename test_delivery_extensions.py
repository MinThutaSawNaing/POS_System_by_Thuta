"""Isolated carrier, private proof and ledger-safe archival regressions."""
import io
import unittest
import test_delivery_operations as base
from PIL import Image, ImageDraw


class DeliveryExtensionTests(unittest.TestCase):
    setUpClass = classmethod(base.DeliveryOperationsTests.setUpClass.__func__)
    tearDownClass = classmethod(base.DeliveryOperationsTests.tearDownClass.__func__)
    setUp = base.DeliveryOperationsTests.setUp
    client = base.DeliveryOperationsTests.client
    delivery = base.DeliveryOperationsTests.delivery
    post = base.DeliveryOperationsTests.post

    def image(self, fmt='PNG'):
        stream = io.BytesIO()
        image = Image.new('RGB', (20, 20), 'white')
        ImageDraw.Draw(image).line((2, 2, 15, 15), fill='black', width=2)
        image.save(stream, format=fmt)
        stream.seek(0)
        return stream

    def proof(self, id, kind='photo', signer=''):
        return self.client().post(f'/api/deliveries/{id}/proofs', data={
            'kind': kind, 'signer_name': signer, 'file': (self.image(), 'proof.png')})

    def test_carrier_manual_url_and_scope(self):
        id = self.delivery()
        response = self.post(id, 'carrier', carrier_name='Manual Courier', tracking_code='ABC',
                             tracking_url='https://track.example.com/ABC', status='in transit', note='Called')
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['delivery']['carrier']['tracking_code'], 'ABC')
        for url in ['http://example.com', 'https://user:pass@example.com', 'https://localhost/a',
                    'https://127.0.0.1', 'https://192.168.0.1/a', 'https://example.local/a', 'javascript:alert(1)']:
            self.assertEqual(self.post(id, 'carrier', tracking_url=url).status_code, 400, url)
        self.assertEqual(self.client(branch=self.b).post(f'/api/deliveries/{id}/carrier', json={}).status_code, 404)
        self.assertEqual(self.client(role='cashier').post(f'/api/deliveries/{id}/carrier', json={}).status_code, 403)

    def test_private_sanitized_proof_signature_and_limit(self):
        id = self.delivery()
        response = self.proof(id)
        self.assertEqual(response.status_code, 200, response.json)
        proof = response.json['delivery']['proofs'][0]
        url = proof['url']
        fetched = self.client().get(url)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.headers['Cache-Control'], 'no-store')
        self.assertEqual(fetched.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.app.test_client().get(url).status_code, 401)
        self.assertEqual(self.client(branch=self.b).get(url).status_code, 404)
        self.assertEqual(self.proof(id, 'signature').status_code, 400)
        self.assertEqual(self.proof(id, 'signature', 'Recipient').status_code, 200)
        blank = io.BytesIO()
        Image.new('RGBA', (20, 20), (0, 0, 0, 0)).save(blank, format='PNG')
        blank.seek(0)
        self.assertEqual(self.client().post(f'/api/deliveries/{id}/proofs', data={
            'kind': 'signature', 'signer_name': 'Recipient', 'file': (blank, 'blank.png')}).status_code, 400)
        self.assertEqual(self.client().post(f'/api/deliveries/{id}/proofs', data={
            'kind': 'photo', 'file': (io.BytesIO(b'<svg onload="alert(1)"/>'), 'fake.png')}).status_code, 400)
        self.assertEqual(self.client().post(f'/api/deliveries/{id}/proofs', data={
            'kind': 'photo', 'file': (io.BytesIO(b'x' * (5 * 1024 * 1024 + 1)), 'large.png')}).status_code, 400)
        for _ in range(8):
            self.assertEqual(self.proof(id).status_code, 200)
        self.assertEqual(self.proof(id).status_code, 400)

    def test_delete_terminal_confirmation_and_cod_safety(self):
        active, pending, cancelled = self.delivery(), self.delivery('delivered'), self.delivery('cancelled')
        def delete(id, **data):
            return self.client().delete(f'/api/deliveries/{id}', json=data)
        self.assertEqual(delete(active, confirm=True, reason='Archive').status_code, 400)
        self.assertEqual(delete(pending, confirm=True, reason='Archive').status_code, 400)
        self.assertEqual(delete(cancelled, confirm=True).status_code, 400)
        self.assertEqual(delete(cancelled, confirm='true', reason='Archive').status_code, 400)
        self.assertEqual(self.post(pending, 'collection', amount='105.00', reference='C').status_code, 200)
        self.assertEqual(delete(pending, confirm=True, reason='Archive').status_code, 400)
        self.assertEqual(self.post(pending, 'remittance', amount='105.00', reference='R').status_code, 200)
        self.assertTrue(delete(pending, confirm=True, reason='Archive').json['deleted'])
        self.assertEqual(self.client().get(f'/api/deliveries/{pending}').status_code, 404)
        self.assertEqual(self.post(pending, 'carrier', note='mutate').status_code, 404)
        self.assertEqual(delete(cancelled, confirm=True, reason='Archive').status_code, 200)
        with self.app.app_context():
            cancelled_sale = self.module.db.session.get(self.module.Delivery, cancelled).sale
            transaction_id = cancelled_sale.transaction_id
            self.assertEqual(cancelled_sale.total, 100)
            self.assertIsNotNone(self.module.db.session.get(self.module.Delivery, pending))
            self.assertEqual(self.module.DeliveryEvent.query.filter_by(delivery_id=pending, action='collection').count(), 1)
        sale_response = self.client().get(f'/api/sales/{transaction_id}')
        self.assertEqual(sale_response.status_code, 200, sale_response.json)
        self.assertIsNone(sale_response.json['delivery'])

    def test_non_http_context_attribution_and_immutable_proofs(self):
        id = self.delivery()
        with self.app.app_context():
            m = self.module
            ops = m.app.extensions['delivery_operations']
            delivery = m.db.session.get(m.Delivery, id)
            with self.assertRaises(PermissionError):
                ops.set_carrier(delivery, {'note': 'No actor'})
            ops.set_carrier(delivery, {'note': 'AI update', 'tracking_code': 'NEW-CODE',
                                      'tracking_url': 'https://example.com/private-link'},
                            actor_id=self.user, branch_id=self.a)
            m.db.session.commit()
            event = m.DeliveryEvent.query.filter_by(delivery_id=id, action='carrier_updated').one()
            self.assertEqual(event.actor_id, self.user)
            self.assertIn('NEW-CODE', event.note)
            self.assertNotIn('private-link', event.note)
            self.assertTrue(all(item.actor_id == self.user for item in delivery.events if item.action != 'created'))
            event.note = 'tamper'
            with self.assertRaises(ValueError):
                m.db.session.commit()
            m.db.session.rollback()

    def test_camera_orientation_is_applied_before_metadata_removed(self):
        delivery_id = self.delivery()
        stream = io.BytesIO()
        image = Image.new('RGB', (30, 20), 'white')
        exif = Image.Exif()
        exif[274] = 6
        image.save(stream, format='JPEG', exif=exif)
        stream.seek(0)
        response = self.client().post(f'/api/deliveries/{delivery_id}/proofs', data={
            'kind': 'photo', 'file': (stream, 'camera.jpg')})
        self.assertEqual(response.status_code, 200, response.json)
        proof = self.client().get(response.json['delivery']['proofs'][0]['url'])
        with Image.open(io.BytesIO(proof.data)) as sanitized:
            self.assertEqual(sanitized.size, (20, 30))
            self.assertEqual(dict(sanitized.getexif()), {})

    def test_metadata_stripped_dimensions_and_existing_profile_archive(self):
        from PIL.PngImagePlugin import PngInfo
        id = self.delivery('cancelled')
        metadata = PngInfo()
        metadata.add_text('private', 'GPS secret')
        stream = io.BytesIO()
        Image.new('RGB', (20, 20)).save(stream, format='PNG', pnginfo=metadata)
        stream.seek(0)
        response = self.client().post(f'/api/deliveries/{id}/proofs', data={
            'kind': 'photo', 'file': (stream, 'metadata.png')})
        self.assertEqual(response.status_code, 200, response.json)
        image = Image.open(io.BytesIO(self.client().get(response.json['delivery']['proofs'][0]['url']).data))
        self.assertNotIn('private', image.info)
        self.assertEqual(self.post(id, 'carrier', carrier_name='Courier').status_code, 200)
        self.assertEqual(self.client().delete(f'/api/deliveries/{id}', json={'confirm': True, 'reason': 'Archived'}).status_code, 200)
        with self.app.app_context():
            m = self.module
            profile = m.db.session.get(m.DeliveryProfile, id)
            profile.delete_reason = 'Tampered'
            with self.assertRaises(ValueError):
                m.db.session.commit()
            m.db.session.rollback()
            proof = m.DeliveryProof.query.filter_by(delivery_id=id).one()
            proof.blob = b'tampered'
            with self.assertRaises(ValueError):
                m.db.session.commit()
            m.db.session.rollback()
        other = self.delivery()
        stream = io.BytesIO()
        Image.new('RGB', (4001, 4000)).save(stream, format='PNG')
        stream.seek(0)
        self.assertEqual(self.client().post(f'/api/deliveries/{other}/proofs', data={
            'kind': 'photo', 'file': (stream, 'oversized-dimensions.png')}).status_code, 400)


if __name__ == '__main__':
    unittest.main()