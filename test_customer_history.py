"""Customer purchase history: the sale/customer link and the history endpoint."""

import unittest
import uuid

from app import (
    app, db, Branch, Customer, Debt, Delivery, Product, Sale, SaleItem, User,
)


class CustomerHistoryTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self._ctx = app.app_context()
        self._ctx.push()
        suffix = uuid.uuid4().hex[:8]
        self.branch = Branch(name=f'History Branch {suffix}', code=f'HB{suffix}',
                             is_active=True, is_default=False)
        self.other_branch = Branch(name=f'Other History {suffix}', code=f'HO{suffix}',
                                   is_active=True, is_default=False)
        db.session.add_all([self.branch, self.other_branch])
        db.session.commit()

        self.manager = User.query.filter_by(username='admin').first()
        self.customer = Customer(name=f'History Customer {suffix}', phone='0912345',
                                 email='history@example.com', branch_id=self.branch.id)
        self.other_customer = Customer(name=f'Other Customer {suffix}', branch_id=self.branch.id)
        self.foreign_customer = Customer(name=f'Foreign Customer {suffix}',
                                         branch_id=self.other_branch.id)
        db.session.add_all([self.customer, self.other_customer, self.foreign_customer])
        db.session.commit()

        self.products = [
            Product(name=f'History Cola {suffix}', price=10, stock=50, tax_rate=0,
                    branch_id=self.branch.id),
            Product(name=f'History Bun {suffix}', price=5, stock=50, tax_rate=0,
                    branch_id=self.branch.id),
        ]
        db.session.add_all(self.products)
        db.session.commit()
        self.created_sale_ids = []

    def tearDown(self):
        sale_ids = set(self.created_sale_ids)
        sale_ids.update(row[0] for row in db.session.query(Debt.sale_id).filter(
            Debt.customer_id.in_([self.customer.id, self.other_customer.id])
        ).all() if row[0])
        sale_ids.update(row[0] for row in db.session.query(Delivery.sale_id).filter(
            Delivery.customer_id.in_([self.customer.id, self.other_customer.id])
        ).all() if row[0])
        sale_ids.update(row[0] for row in db.session.query(Sale.id).filter(
            Sale.customer_id.in_([self.customer.id, self.other_customer.id])
        ).all())

        Delivery.query.filter(Delivery.sale_id.in_(sale_ids or [0])).delete(synchronize_session=False)
        Debt.query.filter(Debt.customer_id.in_([self.customer.id, self.other_customer.id])
                          ).delete(synchronize_session=False)
        SaleItem.query.filter(SaleItem.sale_id.in_(sale_ids or [0])).delete(synchronize_session=False)
        Sale.query.filter(Sale.id.in_(sale_ids or [0])).delete(synchronize_session=False)
        Customer.query.filter(Customer.id.in_([
            self.customer.id, self.other_customer.id, self.foreign_customer.id,
        ])).delete(synchronize_session=False)
        Product.query.filter(Product.id.in_([product.id for product in self.products])
                             ).delete(synchronize_session=False)
        db.session.delete(self.branch)
        db.session.delete(self.other_branch)
        db.session.commit()
        self._ctx.pop()

    def _client(self, role='manager', branch=None):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.manager.id
            session['role'] = role
            session['branch_id'] = (branch or self.branch).id
        return client

    def _sale(self, transaction_id, total, customer_id=None, product=None, quantity=1):
        sale = Sale(transaction_id=transaction_id, total=total, tax=0,
                    payment_method='cash', customer_id=customer_id,
                    user_id=self.manager.id, branch_id=self.branch.id)
        db.session.add(sale)
        db.session.flush()
        db.session.add(SaleItem(sale_id=sale.id, product_id=product.id, quantity=quantity,
                                price=float(product.price), tax=0))
        db.session.commit()
        self.created_sale_ids.append(sale.id)
        return sale


    def test_checkout_stores_the_selected_customer_on_the_sale(self):
        client = self._client()
        response = client.post('/api/sales', json={
            'transaction_id': f'HIST-{uuid.uuid4().hex[:10]}',
            'items': [{'product_id': self.products[0].id, 'price': 10, 'quantity': 2}],
            'payment_method': 'cash',
            'cash_received': 20,
            'customer_id': self.customer.id,
        })
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        sale = Sale.query.filter_by(transaction_id=response.get_json()['transaction_id']).one()
        self.created_sale_ids.append(sale.id)
        self.assertEqual(sale.customer_id, self.customer.id)

    def test_history_lists_what_the_customer_bought(self):
        cash_sale = self._sale(f'HIST-CASH-{uuid.uuid4().hex[:6]}', 20,
                               customer_id=self.customer.id,
                               product=self.products[0], quantity=2)

        # A legacy credit sale has no Sale.customer_id, only the debt link.
        credit_sale = self._sale(f'HIST-CREDIT-{uuid.uuid4().hex[:6]}', 5,
                                 product=self.products[1], quantity=1)
        db.session.add(Debt(customer_id=self.customer.id, sale_id=credit_sale.id,
                            amount=5, balance=3, branch_id=self.branch.id))

        # So does a delivered sale.
        delivery_sale = self._sale(f'HIST-DLV-{uuid.uuid4().hex[:6]}', 10,
                                   product=self.products[0], quantity=1)
        db.session.add(Delivery(delivery_number=f'DLV-{uuid.uuid4().hex[:8]}',
                                sale_id=delivery_sale.id, customer_id=self.customer.id,
                                branch_id=self.branch.id))

        # Another customer's purchase must never leak into this history.
        self._sale(f'HIST-OTHER-{uuid.uuid4().hex[:6]}', 99,
                   customer_id=self.other_customer.id, product=self.products[0])
        db.session.commit()

        response = self._client().get(f'/api/customers/{self.customer.id}/history')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = response.get_json()

        self.assertTrue(data['success'])
        self.assertEqual(data['customer']['name'], self.customer.name)
        self.assertEqual(
            {purchase['transaction_id'] for purchase in data['purchases']},
            {cash_sale.transaction_id, credit_sale.transaction_id, delivery_sale.transaction_id},
        )
        self.assertEqual(data['summary']['visits'], 3)
        self.assertEqual(data['summary']['total_spent'], 35.0)
        self.assertEqual(data['summary']['total_items'], 4)
        self.assertEqual(data['summary']['outstanding_debt'], 3.0)

        cash_entry = next(purchase for purchase in data['purchases']
                          if purchase['transaction_id'] == cash_sale.transaction_id)
        self.assertEqual(cash_entry['items'][0]['name'], self.products[0].name)
        self.assertEqual(cash_entry['items'][0]['quantity'], 2)
        self.assertEqual(cash_entry['items'][0]['line_total'], 20.0)
        self.assertEqual(cash_entry['payment_method'], 'cash')

        self.assertEqual(data['top_products'][0]['name'], self.products[0].name)
        self.assertEqual(data['top_products'][0]['quantity'], 3)

    def test_history_respects_paging_limit(self):
        for index in range(3):
            self._sale(f'HIST-PAGE-{index}-{uuid.uuid4().hex[:6]}', 10 + index,
                       customer_id=self.customer.id, product=self.products[0])

        data = self._client().get(
            f'/api/customers/{self.customer.id}/history?limit=2'
        ).get_json()

        self.assertEqual(len(data['purchases']), 2)
        self.assertTrue(data['has_more'])
        self.assertEqual(data['summary']['visits'], 3)

    def test_legacy_links_require_matching_branches_and_do_not_override_customer(self):
        wrong_debt = self._sale(f'BAD-D-{uuid.uuid4().hex}', 10, product=self.products[0])
        wrong_delivery = self._sale(f'BAD-L-{uuid.uuid4().hex}', 10, product=self.products[0])
        conflicting = self._sale(f'BAD-C-{uuid.uuid4().hex}', 10,
                                 customer_id=self.other_customer.id, product=self.products[0])
        foreign_sale = self._sale(f'BAD-S-{uuid.uuid4().hex}', 10, product=self.products[0])
        foreign_sale.branch_id = self.other_branch.id
        db.session.add_all([
            Debt(customer_id=self.customer.id, sale_id=wrong_debt.id, amount=10, balance=0,
                 branch_id=self.other_branch.id),
            Delivery(delivery_number=f'BAD-{uuid.uuid4().hex}', sale_id=wrong_delivery.id,
                     customer_id=self.customer.id, branch_id=self.other_branch.id),
            Debt(customer_id=self.customer.id, sale_id=conflicting.id, amount=10, balance=0,
                 branch_id=self.branch.id),
            Debt(customer_id=self.customer.id, sale_id=foreign_sale.id, amount=10, balance=0,
                 branch_id=self.branch.id),
        ])
        db.session.commit()
        data = self._client().get(f'/api/customers/{self.customer.id}/history').get_json()
        self.assertEqual(data['summary']['visits'], 0)
        self.assertEqual(data['summary']['total_items'], 0)
        self.assertEqual(data['purchases'], [])

    def test_summary_and_top_products_cover_history_beyond_page(self):
        for index in range(8):
            self._sale(f'ALL-{index}-{uuid.uuid4().hex}', 10,
                       customer_id=self.customer.id, product=self.products[0], quantity=2)
        data = self._client().get(
            f'/api/customers/{self.customer.id}/history?limit=1').get_json()
        self.assertEqual(len(data['purchases']), 1)
        self.assertEqual(data['summary']['visits'], 8)
        self.assertEqual(data['summary']['total_spent'], 80)
        self.assertEqual(data['summary']['total_items'], 16)
        self.assertEqual(data['top_products'][0]['quantity'], 16)
        self.assertTrue(data['has_more'])

    def test_history_is_empty_for_a_customer_without_purchases(self):
        data = self._client().get(f'/api/customers/{self.other_customer.id}/history').get_json()
        self.assertEqual(data['purchases'], [])
        self.assertEqual(data['summary']['visits'], 0)
        self.assertEqual(data['summary']['total_spent'], 0.0)
        self.assertIsNone(data['summary']['last_purchase'])

    def test_history_rejects_a_customer_from_another_branch(self):
        response = self._client().get(f'/api/customers/{self.foreign_customer.id}/history')
        self.assertEqual(response.status_code, 404)

    def test_history_requires_manager_access(self):
        response = self._client(role='cashier').get(f'/api/customers/{self.customer.id}/history')
        self.assertEqual(response.status_code, 403)

    def test_deleting_a_customer_keeps_the_sale_but_unlinks_it(self):
        sale = self._sale(f'HIST-DEL-{uuid.uuid4().hex[:6]}', 10,
                          customer_id=self.customer.id, product=self.products[0])
        sale_id = sale.id
        customer_id = self.customer.id

        response = self._client().delete(f'/api/customers/{customer_id}')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        db.session.expire_all()
        kept = db.session.get(Sale, sale_id)
        self.assertIsNotNone(kept)
        self.assertIsNone(kept.customer_id)
        self.assertIsNone(db.session.get(Customer, customer_id))


if __name__ == '__main__':
    unittest.main()
