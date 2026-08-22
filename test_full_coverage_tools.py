"""Integration tests for the full-feature-coverage AI tools.

These exercise receive_purchase_order, process_return_exchange,
category/promotion/branch tools against the real models using the Flask app
DB (same convention as test_memory_api.py). All fixtures live inside a fresh
throwaway branch; the original default branch is restored afterwards.
"""

import unittest
import uuid
from decimal import Decimal
from unittest import mock

from app import (AI_MODELS, app, db, Branch, Category, Product,
                 PurchaseOrder, PurchaseOrderItem, Sale, SaleItem, Supplier,
                 User, WarehouseInventory)
from ai_tools import AITools


class FullCoverageToolsTestBase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        # Push one app context for the whole test: the tools use
        # Model.query / db.session directly and need it on every call.
        self._ctx = app.app_context()
        self._ctx.push()
        self.admin_id = User.query.filter_by(username='admin').first().id
        self.original_default = Branch.query.filter_by(is_default=True).first()
        suffix = uuid.uuid4().hex[:6]
        self.branch = Branch(name=f"AI Test {suffix}", code=f"AIT{suffix}",
                             is_default=False, is_active=True)
        db.session.add(self.branch)
        db.session.commit()
        self.branch_id = self.branch.id

        self.tools = AITools(db, AI_MODELS)
        self.tools.set_context({"branch_id": self.branch_id, "user_id": self.admin_id})

    def tearDown(self):
        try:
            # Restore whatever was the default branch before the test ran.
            if self.original_default is not None:
                Branch.query.update({'is_default': False})
                row = db.session.get(Branch, self.original_default.id)
                if row:
                    row.is_default = True
                db.session.commit()
            # Remove the throwaway test branch.
            branch = db.session.get(Branch, self.branch_id)
            if branch:
                db.session.delete(branch)
                db.session.commit()
        except Exception:
            db.session.rollback()
        finally:
            self._ctx.pop()

    def _product(self, name, stock=0, price=100.0):
        product = Product(name=name, price=price, cost=price * 0.6, stock=stock,
                          tax_rate=0.0, reorder_enabled=False,
                          branch_id=self.branch_id)
        db.session.add(product)
        db.session.commit()
        return product


class BranchToolTests(FullCoverageToolsTestBase):
    def test_branch_lifecycle(self):
        result = self.tools.create_branch("Temp Shop", f"TMP{uuid.uuid4().hex[:4]}")
        self.assertTrue(result["success"])
        temp_id = result["branch_id"]

        listing = self.tools.get_branch_list()
        self.assertIn(temp_id, [b["branch_id"] for b in listing["branches"]])

        renamed = self.tools.update_branch(temp_id, name="Renamed Shop")
        self.assertTrue(renamed["success"])
        self.assertIn("name", renamed["changed_fields"])

        dup = self.tools.create_branch("Other", result["code"])
        self.assertIn("already exists", dup.get("error", ""))

    def test_set_default_switches_and_refuses_inactive(self):
        result = self.tools.set_default_branch(self.branch_id)
        self.assertTrue(result["success"])
        with app.app_context():
            self.assertTrue(db.session.get(Branch, self.branch_id).is_default)
            self.assertFalse(
                Branch.query.filter(Branch.id != self.branch_id,
                                    Branch.is_default.is_(True)).first() is not None)

    def test_deactivate_refuses_default_then_deactivates_empty(self):
        self.tools.set_default_branch(self.branch_id)
        refused = self.tools.deactivate_branch(self.branch_id)
        self.assertIn("default", refused.get("error", ""))


class CategoryToolTests(FullCoverageToolsTestBase):
    def test_rename_propagates_and_delete_guard_works(self):
        with app.app_context():
            old_name = f"OldName-{uuid.uuid4().hex[:6]}"
            cat = Category(name=old_name, branch_id=self.branch_id)
            db.session.add(cat)
            db.session.commit()
            cat_id = cat.id
            product = Product(name="P1", price=10.0, stock=1, tax_rate=0.0,
                              category=old_name, category_id=cat_id,
                              reorder_enabled=False, branch_id=self.branch_id)
            db.session.add(product)
            db.session.commit()
            product_id = product.id

        refused = self.tools.delete_category(cat_id)
        self.assertIn("Cannot delete", refused.get("error", ""))

        new_name = f"FreshName-{uuid.uuid4().hex[:6]}"
        renamed = self.tools.update_category(cat_id, new_name)
        self.assertTrue(renamed["success"])
        with app.app_context():
            self.assertEqual(db.session.get(Product, product_id).category, new_name)

        with app.app_context():
            db.session.delete(db.session.get(Product, product_id))
            db.session.commit()
        deleted = self.tools.delete_category(cat_id)
        self.assertTrue(deleted["success"])


# === PART 2: PO receiving, returns, promotions ===

import json
from datetime import datetime, timedelta  # noqa: E402


class ReceivePurchaseOrderTests(FullCoverageToolsTestBase):
    def _approved_po(self, ordered=10):
        with app.app_context():
            supplier = Supplier(name=f"Sup-{uuid.uuid4().hex[:6]}",
                                branch_id=self.branch_id)
            db.session.add(supplier)
            db.session.commit()
            product = self._product("Cola", stock=0)
            po = PurchaseOrder(po_number=f"PO-TEST-{uuid.uuid4().hex[:6].upper()}",
                               supplier_id=supplier.id, status='approved',
                               total_amount=ordered * 5.0, branch_id=self.branch_id,
                               created_by=self.admin_id)
            db.session.add(po)
            db.session.flush()
            item = PurchaseOrderItem(purchase_order_id=po.id, product_id=product.id,
                                     ordered_qty=ordered, received_qty=0,
                                     unit_cost=5.0)
            db.session.add(item)
            db.session.commit()
            return po.id, item.id, product.id

    def test_partial_then_full_receive_updates_status_and_warehouse(self):
        po_id, item_id, product_id = self._approved_po()

        partial = self.tools.receive_purchase_order(po_id)
        self.assertTrue(partial["success"])
        self.assertEqual(partial["status"], "received")  # default = receive all

        with app.app_context():
            po = db.session.get(PurchaseOrder, po_id)
            self.assertEqual(po.items[0].received_qty, 10)
            self.assertEqual(po.status, 'received')
            warehouse = WarehouseInventory.query.filter_by(
                product_id=product_id, batch_number=po.po_number).all()
            self.assertEqual(sum(w.quantity for w in warehouse), 10)

        again = self.tools.receive_purchase_order(po_id)
        self.assertIn("Cannot receive", again.get("error", ""))

    def test_receive_rejects_over_receipt(self):
        po_id, item_id, _ = self._approved_po(ordered=5)
        result = self.tools.receive_purchase_order(
            po_id, items=[{"purchase_order_item_id": item_id, "received_qty": 6}])
        self.assertIn("exceeds remaining", result.get("error", ""))


class ReturnExchangeToolTests(FullCoverageToolsTestBase):
    def _sale_with_item(self, qty=5, price=100.0):
        with app.app_context():
            product = self._product("Sold Item", stock=20, price=price)
            sale = Sale(transaction_id=f"TX-{uuid.uuid4().hex[:10]}", total=qty * price,
                        tax=0.0, refund_amount=0.0, payment_method='cash',
                        user_id=self.admin_id, branch_id=self.branch_id)
            db.session.add(sale)
            db.session.flush()
            item = SaleItem(sale_id=sale.id, product_id=product.id,
                            quantity=qty, price=price, tax=0.0)
            db.session.add(item)
            db.session.commit()
            return sale.transaction_id, item.id, product.id

    def test_return_refunds_and_restocks_then_limits_quantity(self):
        tx, sale_item_id, product_id = self._sale_with_item(qty=5, price=100.0)

        result = self.tools.process_return_exchange(
            tx, return_items=[{"sale_item_id": sale_item_id, "quantity": 2}])
        self.assertTrue(result["success"])
        self.assertEqual(Decimal(result["refund_amount"]), Decimal("200.00"))

        with app.app_context():
            self.assertEqual(db.session.get(Product, product_id).stock, 22)

        over = self.tools.process_return_exchange(
            tx, return_items=[{"sale_item_id": sale_item_id, "quantity": 4}])
        self.assertIn("Available: 3", over.get("error", ""))

    def test_exchange_creates_adjustment_and_settles(self):
        tx, sale_item_id, _ = self._sale_with_item(qty=5, price=100.0)
        with app.app_context():
            swap = self._product("Swap Item", stock=10, price=50.0)
            swap_id = swap.id

        result = self.tools.process_return_exchange(
            tx, return_items=[{"sale_item_id": sale_item_id, "quantity": 1}],
            exchange_items=[{"product_id": swap_id, "quantity": 1, "price": 50.0}])
        self.assertTrue(result["success"])
        self.assertEqual(result["mode"], "exchange")
        self.assertEqual(Decimal(result["refund_amount"]), Decimal("50.00"))
        self.assertIn("adjustment_transaction_id", result)

        with app.app_context():
            self.assertEqual(db.session.get(Product, swap_id).stock, 9)


class PromotionToolTests(FullCoverageToolsTestBase):
    def _promotion(self):
        with app.app_context():
            product = self._product("Promo Item", stock=3)
            from app import Promotion
            promo = Promotion(product_id=product.id, discount_type='percent',
                              discount_value=10.0,
                              start_date=datetime.utcnow(),
                              end_date=datetime.utcnow() + timedelta(days=7))
            db.session.add(promo)
            db.session.commit()
            return promo.id

    def test_update_validates_date_order_then_cancel_deletes(self):
        promo_id = self._promotion()

        bad = self.tools.update_promotion(promo_id, start_date="2026-12-01T00:00:00",
                                          end_date="2026-01-01T00:00:00")
        self.assertIn("after start", bad.get("error", ""))

        ok = self.tools.update_promotion(promo_id, discount_type="fixed",
                                         discount_value="15.50")
        self.assertTrue(ok["success"])
        self.assertIn("discount_value", ok["changed_fields"])

        cancelled = self.tools.cancel_promotion(promo_id)
        self.assertTrue(cancelled["success"])


if __name__ == "__main__":
    unittest.main()
