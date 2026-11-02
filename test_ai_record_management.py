"""AI history maintenance uses the same authorization and atomic services as HTTP."""

import unittest

import test_ai_shared_workflows as shared
from ai_tools import AITools, TOOL_METADATA
from agent_orchestrator import AgentOrchestrator, SYSTEM_PROMPT


class AIRecordManagementTests(unittest.TestCase):
    setUpClass = classmethod(shared.SharedAIWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(shared.SharedAIWorkflowTests.tearDownClass.__func__)
    setUp = shared.SharedAIWorkflowTests.setUp
    tearDown = shared.SharedAIWorkflowTests.tearDown

    def sale(self, transaction='AI-SALE', branch=None):
        m = self.module
        sale = m.Sale(transaction_id=transaction, total=10, tax=0,
                      user_id=self.user, branch_id=branch or self.a,
                      payment_method='cash', cash_received=10)
        m.db.session.add(sale)
        m.db.session.flush()
        m.db.session.add(m.SaleItem(sale_id=sale.id, product_id=self.local,
                                   quantity=1, price=10, tax=0))
        m.db.session.commit()
        return sale.id

    def test_tools_registered_as_writes_and_filtered_for_topics(self):
        orch = AgentOrchestrator(None, {})
        for query, names in (
            ('edit purchase order', {'update_purchase_order', 'delete_purchase_order'}),
            ('delete sale history', {'delete_sale_history'}),
        ):
            offered = {schema['function']['name'] for schema in orch._filter_tools_for_query(query)}
            self.assertTrue(names <= offered)
            for name in names:
                self.assertTrue(TOOL_METADATA[name]['mutates'])
                self.assertTrue(callable(getattr(self.tools, name)))
        self.assertIn('HISTORY-ONLY', SYSTEM_PROMPT)
        self.assertIn('confirm=true only when a later user reply', SYSTEM_PROMPT)

    def test_edit_shared_po_and_delete_without_affecting_stock(self):
        m, t = self.module, self.tools
        stock = m.db.session.get(m.Product, self.foreign).stock
        result = t.update_purchase_order(self.po, notes='Updated by AI', items=[
            {'product_id': self.foreign, 'quantity': 3, 'unit_cost': 7.25}])
        self.assertTrue(result['success'], result)
        self.assertEqual(result['total_amount'], '21.75')
        po = m.db.session.get(m.PurchaseOrder, self.po)
        self.assertEqual(po.notes, 'Updated by AI')
        self.assertEqual(po.items[0].ordered_qty, 3)
        self.assertFalse(t.delete_purchase_order(self.po).get('success'))
        self.assertIsNotNone(m.db.session.get(m.PurchaseOrder, self.po))
        result = t.delete_purchase_order(self.po, confirm=True)
        self.assertTrue(result['success'], result)
        self.assertIsNone(m.db.session.get(m.PurchaseOrder, self.po))
        self.assertEqual(m.db.session.get(m.Product, self.foreign).stock, stock)

    def test_delete_sale_requires_confirmation_and_preserves_stock(self):
        m, t = self.module, self.tools
        sale_id = self.sale()
        stock = m.db.session.get(m.Product, self.local).stock
        self.assertFalse(t.delete_sale_history('AI-SALE').get('success'))
        self.assertIsNotNone(m.db.session.get(m.Sale, sale_id))
        result = t.delete_sale_history('AI-SALE', confirm=True)
        self.assertTrue(result['success'], result)
        self.assertIsNone(m.db.session.get(m.Sale, sale_id))
        self.assertEqual(m.SaleItem.query.filter_by(sale_id=sale_id).count(), 0)
        self.assertEqual(m.db.session.get(m.Product, self.local).stock, stock)

    def test_invalid_po_edit_does_not_partially_update_notes(self):
        m = self.module
        result = self.tools.update_purchase_order(self.po, notes='Do not save', items=[
            {'product_id': self.foreign, 'quantity': 0, 'unit_cost': 7.25}])
        self.assertFalse(result['success'], result)
        self.assertIsNone(m.db.session.get(m.PurchaseOrder, self.po).notes)

    def test_received_po_only_allows_notes_and_date(self):
        m = self.module
        po = m.db.session.get(m.PurchaseOrder, self.po)
        po.status = 'received'
        po.items[0].received_qty = po.items[0].ordered_qty
        m.db.session.commit()
        result = self.tools.update_purchase_order(self.po, notes='Receipt checked',
                                                  expected_delivery_date='2026-11-02')
        self.assertTrue(result['success'], result)
        result = self.tools.update_purchase_order(self.po, items=[
            {'product_id': self.foreign, 'quantity': 10, 'unit_cost': 7.25}])
        self.assertFalse(result['success'], result)
        self.assertEqual(m.db.session.get(m.PurchaseOrder, self.po).notes, 'Receipt checked')

    def test_cashier_and_unknown_scopes_cannot_use_new_operations(self):
        m = self.module
        for scope in ({}, {'role': 'cashier', 'user_id': self.user, 'branch_id': self.a}):
            t = AITools(m.db, m.AI_MODELS)
            t.set_context(scope)
            for result in (t.update_purchase_order(self.po, notes='Forbidden'),
                           t.delete_purchase_order(self.po, confirm=True),
                           t.delete_sale_history('AI-SALE', confirm=True)):
                self.assertFalse(result.get('success'), result)
                self.assertIn('access required', result['error'])

    def test_forged_manager_scope_cannot_promote_persisted_cashier(self):
        m = self.module
        cashier = m.User(username='no-history-access', password='unused',
                         role='cashier', branch_id=self.a)
        m.db.session.add(cashier)
        m.db.session.commit()
        self.tools.set_context({'role': 'manager', 'user_id': cashier.id, 'branch_id': self.a})
        result = self.tools.delete_purchase_order(self.po, confirm=True)
        self.assertFalse(result['success'])
        self.assertIsNotNone(m.db.session.get(m.PurchaseOrder, self.po))

    def test_http_cashier_session_overrides_cached_manager_context(self):
        from flask import session
        with self.app.test_request_context('/'):
            session.update(user_id=self.user, role='cashier', branch_id=self.a)
            result = self.tools.delete_purchase_order(self.po, confirm=True)
        self.assertFalse(result['success'])

    def test_sale_delete_is_branch_local_and_missing_branch_fails_closed(self):
        m = self.module
        foreign_sale = self.sale('FOREIGN-AI-SALE', branch=self.b)
        result = self.tools.delete_sale_history('FOREIGN-AI-SALE', confirm=True)
        self.assertFalse(result['success'])
        self.assertIsNotNone(m.db.session.get(m.Sale, foreign_sale))
        self.tools.set_context({'role': 'manager', 'user_id': self.user})
        self.assertIn('active branch', self.tools.delete_sale_history('FOREIGN-AI-SALE', True)['error'])

    def test_boss_can_edit_and_delete_po(self):
        m = self.module
        boss = m.User(username='owner-records', password='unused', role='boss', branch_id=self.a)
        m.db.session.add(boss)
        m.db.session.commit()
        self.tools.set_context({'role': 'boss', 'user_id': boss.id, 'branch_id': self.a})
        self.assertTrue(self.tools.update_purchase_order(self.po, notes='Boss edit')['success'])
        self.assertTrue(self.tools.delete_purchase_order(self.po, confirm=True)['success'])
        self.sale('BOSS-AI-SALE')
        self.assertTrue(self.tools.delete_sale_history('BOSS-AI-SALE', confirm=True)['success'])