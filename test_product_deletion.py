"""API regression tests for product deletion safeguards.

Covers the manager-only gate, the dependency summary that powers the
"delete everywhere" window, and the cascade that clears warehouse/promotion/
purchase-order records while always keeping sales history.
"""

import unittest
import uuid
from datetime import datetime, timedelta

from app import (
    app, db, Branch, Product, Promotion, PurchaseOrder, PurchaseOrderItem,
    ReturnExchange, ReturnExchangeItem, Sale, SaleItem, Supplier,
    SupplierPriceAgreement, User, WarehouseInventory, WarehouseTransfer,
)


class ProductDeletionTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self._ctx = app.app_context()
        self._ctx.push()
        self.branch = Branch.query.filter_by(is_active=True).first()
        self.user = User.query.filter_by(username='admin').first()
        self.product_ids = []
        self.sale_ids = []
        self.sale_item_ids = []
        self.promotion_ids = []
        self.warehouse_ids = []
        self.transfer_ids = []
        self.agreement_ids = []
        self.supplier_ids = []
        self.purchase_order_ids = []
        self.return_ids = []

    def tearDown(self):
        ReturnExchangeItem.query.filter(
            ReturnExchangeItem.return_exchange_id.in_(self.return_ids or [0])
        ).delete(synchronize_session=False)
        ReturnExchange.query.filter(ReturnExchange.id.in_(self.return_ids or [0])).delete(
            synchronize_session=False
        )
        SaleItem.query.filter(SaleItem.sale_id.in_(self.sale_ids or [0])).delete(
            synchronize_session=False
        )
        SaleItem.query.filter(SaleItem.id.in_(self.sale_item_ids or [0])).delete(
            synchronize_session=False
        )
        Sale.query.filter(Sale.id.in_(self.sale_ids or [0])).delete(
            synchronize_session=False
        )
        PurchaseOrderItem.query.filter(
            PurchaseOrderItem.purchase_order_id.in_(self.purchase_order_ids or [0])
        ).delete(synchronize_session=False)
        PurchaseOrder.query.filter(
            PurchaseOrder.id.in_(self.purchase_order_ids or [0])
        ).delete(synchronize_session=False)
        Supplier.query.filter(Supplier.id.in_(self.supplier_ids or [0])).delete(
            synchronize_session=False
        )
        Promotion.query.filter(Promotion.id.in_(self.promotion_ids or [0])).delete(
            synchronize_session=False
        )
        WarehouseInventory.query.filter(
            WarehouseInventory.id.in_(self.warehouse_ids or [0])
        ).delete(synchronize_session=False)
        WarehouseTransfer.query.filter(
            WarehouseTransfer.id.in_(self.transfer_ids or [0])
        ).delete(synchronize_session=False)
        SupplierPriceAgreement.query.filter(
            SupplierPriceAgreement.id.in_(self.agreement_ids or [0])
        ).delete(synchronize_session=False)
        Product.query.filter(Product.id.in_(self.product_ids or [0])).delete(
            synchronize_session=False
        )
        db.session.commit()
        self._ctx.pop()

    def _client(self, role='manager'):
        client = app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user.id
            session['role'] = role
            session['branch_id'] = self.branch.id
        return client

    def _product(self, name='Delete Test Product', price=100.0):
        product = Product(
            name=f'{name} {uuid.uuid4().hex}', price=price, stock=1,
            tax_rate=0.0, branch_id=self.branch.id,
        )
        db.session.add(product)
        db.session.commit()
        self.product_ids.append(product.id)
        return product

    def _sale_with_item(self, product, quantity=1, price=100.0, tax=0.0):
        sale = Sale(
            transaction_id=f'product-delete-{uuid.uuid4().hex}',
            total=price * quantity, tax=tax, payment_method='cash',
            user_id=self.user.id, branch_id=self.branch.id,
        )
        db.session.add(sale)
        db.session.flush()
        item = SaleItem(
            sale_id=sale.id, product_id=product.id, quantity=quantity, price=price,
            tax=tax,
        )
        db.session.add(item)
        db.session.commit()
        self.sale_ids.append(sale.id)
        self.sale_item_ids.append(item.id)
        return sale, item

    def _attach_catalog_records(self, product):
        """Wire the product into warehouse, promotions, prices and a PO pair."""
        supplier = Supplier(name=f'Del Sup {uuid.uuid4().hex[:6]}',
                            branch_id=self.branch.id)
        db.session.add(supplier)
        db.session.flush()
        self.supplier_ids.append(supplier.id)

        now = datetime.utcnow()
        warehouse = WarehouseInventory(
            product_id=product.id, quantity=5, location='A1',
            branch_id=self.branch.id,
        )
        transfer = WarehouseTransfer(
            product_id=product.id, quantity=2, from_warehouse=True,
            branch_id=self.branch.id,
        )
        promotion = Promotion(
            product_id=product.id, discount_type='percent', discount_value=10,
            start_date=now - timedelta(days=1), end_date=now + timedelta(days=1),
        )
        agreement = SupplierPriceAgreement(
            supplier_id=supplier.id, product_id=product.id, agreed_price=80.0,
        )
        db.session.add_all([warehouse, transfer, promotion, agreement])

        other_product = self._product('Other Product', price=50.0)
        purchase_order = PurchaseOrder(
            po_number=f'PO-{uuid.uuid4().hex[:10].upper()}', supplier_id=supplier.id,
            status='approved', total_amount=0.0, branch_id=self.branch.id,
        )
        db.session.add(purchase_order)
        db.session.flush()
        db.session.add_all([
            PurchaseOrderItem(purchase_order_id=purchase_order.id,
                              product_id=product.id, ordered_qty=4,
                              received_qty=0, unit_cost=25.0),
            PurchaseOrderItem(purchase_order_id=purchase_order.id,
                              product_id=other_product.id, ordered_qty=2,
                              received_qty=0, unit_cost=30.0),
        ])
        purchase_order.total_amount = 4 * 25.0 + 2 * 30.0
        db.session.commit()

        self.warehouse_ids.append(warehouse.id)
        self.transfer_ids.append(transfer.id)
        self.promotion_ids.append(promotion.id)
        self.agreement_ids.append(agreement.id)
        self.purchase_order_ids.append(purchase_order.id)
        return purchase_order.id, other_product.id

    def _return_for(self, product):
        sale, item = self._sale_with_item(product)
        workflow = ReturnExchange(
            workflow_id=str(uuid.uuid4()), mode='return',
            original_sale_id=sale.id, return_total=100.0, exchange_total=0.0,
            net_total=-100.0, refund_amount=100.0, collected_amount=0.0,
            settlement_method='cash', user_id=self.user.id,
        )
        db.session.add(workflow)
        db.session.flush()
        db.session.add(ReturnExchangeItem(
            return_exchange_id=workflow.id, original_sale_item_id=item.id,
            product_id=product.id, movement='return', quantity=1,
            unit_price=100.0, tax_rate=0.0, line_total=100.0, line_tax=0.0,
        ))
        db.session.commit()
        self.return_ids.append(workflow.id)
        return workflow

    def test_manager_can_delete_unused_product(self):
        product = self._product()

        response = self._client().delete(f'/api/products/{product.id}')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        self.assertIsNone(db.session.get(Product, product.id))

    def test_cashier_cannot_delete_products(self):
        product = self._product()

        response = self._client('cashier').delete(f'/api/products/{product.id}')

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('manager', response.get_json()['message'])
        self.assertIsNotNone(db.session.get(Product, product.id))

    def test_cashier_cannot_read_dependency_summary(self):
        product = self._product()

        response = self._client('cashier').get(f'/api/products/{product.id}/dependencies')

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.get_json()['success'])

    def test_boss_can_delete_products(self):
        product = self._product()

        response = self._client('boss').delete(f'/api/products/{product.id}')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        self.assertIsNone(db.session.get(Product, product.id))

    def test_product_with_sales_history_is_not_deleted_without_force(self):
        product = self._product('Sold Product')
        self._sale_with_item(product)

        response = self._client().delete(f'/api/products/{product.id}')

        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertIn('sales history', body['message'])
        self.assertTrue(body['has_sales_history'])
        self.assertTrue(body['requires_confirmation'])
        self.assertEqual(body['sales_history_count'], 1)
        self.assertIsNotNone(db.session.get(Product, product.id))

    def test_confirming_sales_history_deletes_product_and_keeps_sale(self):
        product = self._product('Sold Product')
        sale, item = self._sale_with_item(product)
        client = self._client()

        response = client.delete(f'/api/products/{product.id}?force=1')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        self.assertIsNone(db.session.get(Product, product.id))
        item = db.session.get(SaleItem, item.id)
        self.assertIsNone(item.product_id)
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.price, 100.0)
        self.assertEqual(db.session.get(Sale, sale.id).total, 100.0)

    def test_confirmed_deletion_keeps_sales_history_view_working(self):
        product = self._product('Sold Product')
        sale, item = self._sale_with_item(product)
        client = self._client()

        client.delete(f'/api/products/{product.id}?force=1')

        response = client.get(f'/api/sales/{sale.transaction_id}')

        self.assertEqual(response.status_code, 200)
        items = response.get_json()['items']
        line = next(row for row in items if row['sale_item_id'] == item.id)
        self.assertEqual(line['name'], 'Deleted product')
        self.assertEqual(line['price'], 100.0)
        self.assertEqual(line['available_return_quantity'], 0)

    def test_dependency_summary_lists_every_tab_with_its_action(self):
        product = self._product('Wired Product')
        self._sale_with_item(product)
        self._attach_catalog_records(product)
        client = self._client()

        response = client.get(f'/api/products/{product.id}/dependencies')

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        actions = {group['key']: group['action'] for group in body['groups']}
        self.assertEqual(actions['warehouse_inventory'], 'delete')
        self.assertEqual(actions['warehouse_transfers'], 'delete')
        self.assertEqual(actions['promotions'], 'delete')
        self.assertEqual(actions['supplier_price_agreements'], 'delete')
        self.assertEqual(actions['purchase_order_items'], 'delete')
        self.assertEqual(actions['sales_history'], 'keep')
        self.assertTrue(body['can_delete'])
        self.assertTrue(body['has_sales_history'])
        self.assertGreater(body['removable_count'], 0)

    def test_other_tab_records_require_the_cascade_confirmation(self):
        product = self._product('Wired Product')
        self._attach_catalog_records(product)

        response = self._client().delete(f'/api/products/{product.id}?force=1')

        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertTrue(body['requires_cascade'])
        self.assertIn('promotions', body['dependencies'])
        self.assertIn('warehouse_inventory', body['dependencies'])
        self.assertIn('purchase_order_items', body['dependencies'])
        self.assertIsNotNone(db.session.get(Product, product.id))

    def test_cascade_delete_clears_other_tabs_and_recalculates_totals(self):
        product = self._product('Wired Product')
        sale, item = self._sale_with_item(product)
        purchase_order_id, _ = self._attach_catalog_records(product)
        client = self._client()

        response = client.delete(f'/api/products/{product.id}?force=1&cascade=1')

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['cleaned_up']['promotions'], 1)
        self.assertEqual(body['cleaned_up']['warehouse_inventory'], 1)
        self.assertEqual(body['cleaned_up']['warehouse_transfers'], 1)
        self.assertEqual(body['cleaned_up']['supplier_price_agreements'], 1)
        self.assertEqual(body['cleaned_up']['purchase_order_items'], 1)
        self.assertIsNone(db.session.get(Product, product.id))
        self.assertEqual(Promotion.query.filter_by(product_id=product.id).count(), 0)
        self.assertEqual(
            WarehouseInventory.query.filter_by(product_id=product.id).count(), 0)
        self.assertEqual(
            WarehouseTransfer.query.filter_by(product_id=product.id).count(), 0)
        self.assertEqual(
            SupplierPriceAgreement.query.filter_by(product_id=product.id).count(), 0)
        self.assertEqual(
            PurchaseOrderItem.query.filter_by(product_id=product.id).count(), 0)
        # The surviving PO line is kept and the order total is recalculated.
        purchase_order = db.session.get(PurchaseOrder, purchase_order_id)
        self.assertEqual(purchase_order.total_amount, 2 * 30.0)
        self.assertEqual(
            PurchaseOrderItem.query.filter_by(
                purchase_order_id=purchase_order_id).count(), 1)
        # Sales history survives untouched apart from the product link.
        self.assertIsNone(db.session.get(SaleItem, item.id).product_id)
        self.assertEqual(db.session.get(Sale, sale.id).total, 100.0)

    def test_returns_block_the_delete_even_with_the_cascade(self):
        product = self._product('Returned Product')
        workflow = self._return_for(product)
        client = self._client()
        self.assertFalse(
            client.get(f'/api/products/{product.id}/dependencies').get_json()['can_delete'])

        response = client.delete(f'/api/products/{product.id}?force=1&cascade=1')

        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertTrue(body['blocked'])
        self.assertEqual(body['blocked_by'], 'returns_exchanges')
        self.assertIn('Returns tab', body['message'])
        self.assertIsNotNone(db.session.get(Product, product.id))
        self.assertEqual(
            ReturnExchangeItem.query.filter_by(product_id=product.id).count(), 1)
        self.assertIsNotNone(db.session.get(ReturnExchange, workflow.id))


if __name__ == '__main__':
    unittest.main()
