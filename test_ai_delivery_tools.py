"""Delivery agent contracts, trusted non-HTTP scope and real shared operations."""
import unittest
from unittest.mock import Mock

from ai_tools import AITools, TOOL_METADATA, get_all_tools
from ai_agent import AIAgent, ToolCall
from agent_orchestrator import AgentOrchestrator, SYSTEM_PROMPT
import test_delivery_operations as backend


class AIDeliveryTests(unittest.TestCase):
    setUpClass = classmethod(backend.DeliveryOperationsTests.setUpClass.__func__)
    tearDownClass = classmethod(backend.DeliveryOperationsTests.tearDownClass.__func__)
    setUp = backend.DeliveryOperationsTests.setUp
    delivery = backend.DeliveryOperationsTests.delivery

    def tools(self, role='manager', branch=None):
        tools = AITools(self.module.db, self.module.AI_MODELS)
        tools.set_context({'user_id': self.user, 'role': role, 'branch_id': branch or self.a})
        return tools

    def test_shared_carrier_attempt_retry_and_actor_without_request(self):
        id = self.delivery()
        with self.app.app_context():
            t = self.tools()
            result = t.update_delivery_carrier(id, carrier_name='Manual courier', tracking_code='T1')
            self.assertTrue(result['success'], result)
            self.assertEqual(result['integration_mode'], 'manual_only')
            self.assertTrue(t.record_delivery_attempt(id, 'failed', reason='Recipient absent')['success'])
            self.assertTrue(t.retry_delivery(id, note='Customer requested tomorrow')['success'])
            details = t.get_delivery_details(id)
            self.assertEqual(details['stage'], 'packaged')
            self.assertEqual(details['carrier']['tracking_code'], 'T1')
            self.assertEqual(details['attempt_count'], 1)
            self.assertEqual(details['proofs'], [])
            self.assertTrue(all(e['actor_id'] == self.user for e in details['history'] if e['action'] != 'created'))
            self.assertIn('secure delivery UI', details['proof_upload'])

    def test_cod_exact_idempotent_collection_remittance_and_no_sale_write(self):
        id = self.delivery()
        with self.app.app_context():
            t = self.tools()
            self.assertTrue(t.record_delivery_attempt(id, 'delivered', proof_note='User reports recipient accepted')['success'])
            self.assertFalse(t.record_delivery_remittance(id, 105, 'bank receipt')['success'])
            self.assertFalse(t.record_delivery_collection(id, 100, 'cash receipt')['success'])
            for _ in range(2):
                self.assertTrue(t.record_delivery_collection(id, 105, 'cash receipt')['success'])
            self.assertTrue(t.record_delivery_remittance(id, 105, 'bank receipt')['success'])
            self.assertEqual(t.get_delivery_details(id)['cod']['status'], 'remitted')
            self.assertEqual(self.module.db.session.get(self.module.Delivery, id).sale.total, 100)

    def test_foreign_cashier_missing_branch_and_http_override(self):
        local, foreign = self.delivery(), self.delivery(branch=self.b)
        with self.app.app_context():
            t = self.tools()
            for result in (t.get_delivery_details(foreign), t.update_delivery_carrier(foreign, carrier_name='No'),
                           t.record_delivery_attempt(foreign, 'failed', reason='No'), t.retry_delivery(foreign),
                           t.record_delivery_collection(foreign, 105, 'No'), t.record_delivery_remittance(foreign, 105, 'No'),
                           t.delete_delivery_record(foreign, reason='No')):
                self.assertIn('not found', result['error'])
            for role in ('cashier', 'staff', None):
                t = self.tools(role)
                self.assertFalse(t.get_delivery_summary().get('success', True))
                self.assertFalse(t.update_delivery_carrier(local, carrier_name='No')['success'])
            t = self.tools()
            t.context.pop('branch_id')
            self.assertIn('active branch', t.get_delivery_summary()['error'])
            from flask import session
            with self.app.test_request_context('/'):
                session.update(user_id=self.user, role='cashier', branch_id=self.a)
                self.assertFalse(self.tools().record_delivery_attempt(local, 'failed', reason='No')['success'])

    def test_delete_preview_approval_softdelete_and_active_queries(self):
        id = self.delivery(stage='cancelled')
        with self.app.app_context():
            t = self.tools()
            self.assertTrue(t.delete_delivery_record(id, reason='Duplicate')['requires_confirmation'])
            self.assertFalse(t.delete_delivery_record(id, confirm=True, reason='Duplicate')['success'])
            t.context['delivery_delete_approval'] = {'delivery_id': id, 'reason': 'Wrong reason'}
            self.assertFalse(t.delete_delivery_record(id, confirm=True, reason='Duplicate')['success'])
            t.context['delivery_delete_approval'] = {'delivery_id': id, 'reason': 'Duplicate'}
            self.assertTrue(t.delete_delivery_record(id, confirm=True, reason='Duplicate')['success'])
            self.assertNotIn('delivery_delete_approval', t.context)
            self.assertEqual(t.get_delivery_summary()['deliveries'], [])
            self.assertIn('not found', t.get_delivery_details(id)['error'])
            self.assertIn('not found', t.update_delivery_stage(id, 'packaged')['error'])
            self.assertIsNotNone(self.module.db.session.get(self.module.Delivery, id).sale)

    def test_boss_parity_and_persisted_cashier_denial(self):
        id = self.delivery()
        with self.app.app_context():
            m = self.module
            boss = m.User(username='owner', password='unused', role='boss', branch_id=self.b)
            m.db.session.add(boss)
            m.db.session.commit()
            t = self.tools('boss')
            t.context['user_id'] = boss.id
            self.assertTrue(t.update_delivery_carrier(id, carrier_name='Boss courier')['success'])
            t.context['user_id'] = self.cashier
            self.assertFalse(t.update_delivery_carrier(id, carrier_name='Forged')['success'])

    def test_actual_orchestrator_later_reply_deletes_only_previewed_record(self):
        id = self.delivery(stage='cancelled')
        with self.app.app_context():
            orch = AgentOrchestrator(self.module.db, self.module.AI_MODELS, app=self.app)
            orch.set_request_context({'user_id': self.user, 'role': 'manager', 'branch_id': self.a})
            orch._execute_tools_with_context([ToolCall('preview', 'delete_delivery_record',
                {'delivery_id': id, 'confirm': False, 'reason': 'User reported duplicate'})])
            result = orch.process_command('confirm')
            self.assertTrue(result['success'], result)
            self.assertEqual(result['refresh_sections'], ['deliveries'])
            self.assertTrue(self.module.db.session.get(self.module.Delivery, id).profile.deleted_at)


class DeliveryPolicyTests(unittest.TestCase):
    def test_registry_intent_and_truthful_limits(self):
        orch = AgentOrchestrator(None, {})
        names = {s['function']['name'] for s in orch._filter_tools_for_query('delivery COD proof carrier')}
        for name in ('get_delivery_details', 'update_delivery_carrier', 'record_delivery_attempt',
                     'retry_delivery', 'record_delivery_collection', 'record_delivery_remittance', 'delete_delivery_record'):
            self.assertIn(name, names)
            self.assertEqual(TOOL_METADATA[name]['requires_role'], ['manager', 'boss'])
        self.assertEqual(TOOL_METADATA['delete_delivery_record']['autonomy'], 'approval')
        self.assertNotIn('delete_delivery_record', get_all_tools(read_only=True))
        self.assertIn('Never fabricate proof', SYSTEM_PROMPT)
        self.assertIn('no live booking', SYSTEM_PROMPT)

    def test_both_executors_reject_cashier_and_unapproved_confirm_even_auto(self):
        for executor in ('agent', 'orchestrator'):
            for role, name, args in (
                ('cashier', 'get_delivery_details', {'delivery_id': 1}),
                ('manager', 'delete_delivery_record', {'delivery_id': 1, 'confirm': True, 'reason': 'Duplicate'})):
                obj = AIAgent() if executor == 'agent' else AgentOrchestrator(None, {})
                obj.set_request_context({'user_id': 1, 'role': role, 'branch_id': 1, 'autonomy': 'auto'})
                agent = obj if executor == 'agent' else obj.agent
                function = Mock(return_value={'success': True})
                agent.tool_functions[name] = function
                calls = [ToolCall('1', name, args)]
                results = obj.execute_tool_calls(calls) if executor == 'agent' else obj._execute_tools_with_context(calls)
                self.assertTrue(results[0]['error'])
                function.assert_not_called()

    def test_preview_later_confirmation_consumes_exact_approval_without_llm(self):
        orch = AgentOrchestrator(None, {})
        orch.set_request_context({'user_id': 1, 'role': 'manager', 'branch_id': 1})
        preview = Mock(return_value={'success': False, 'requires_confirmation': True})
        orch.agent.tool_functions['delete_delivery_record'] = preview
        orch._execute_tools_with_context([ToolCall('preview', 'delete_delivery_record',
            {'delivery_id': 7, 'reason': 'Duplicate', 'confirm': False})])
        captured = []
        def deletion(**kwargs):
            captured.append(dict(orch.ai_tools.context['delivery_delete_approval']))
            return {'success': True}
        orch.ai_tools.delete_delivery_record = deletion
        result = orch.process_command('yes')
        self.assertTrue(result['success'])
        self.assertEqual(captured, [{'delivery_id': 7, 'reason': 'Duplicate'}])
        self.assertNotIn('delivery_delete_pending', orch.session_context)
        self.assertNotIn('delivery_delete_approval', orch.ai_tools.context)

    def test_identity_switch_expires_approval(self):
        orch = AgentOrchestrator(None, {})
        orch.set_request_context({'user_id': 1, 'role': 'manager', 'branch_id': 1})
        orch.agent.tool_functions['delete_delivery_record'] = Mock(return_value={
            'success': False, 'requires_confirmation': True})
        orch._execute_tools_with_context([ToolCall('preview', 'delete_delivery_record',
            {'delivery_id': 7, 'reason': 'Duplicate', 'confirm': False})])
        orch.set_request_context({'user_id': 1, 'role': 'manager', 'branch_id': 2})
        orch.ai_tools.delete_delivery_record = Mock()
        self.assertFalse(orch.process_command('yes')['success'])
        orch.ai_tools.delete_delivery_record.assert_not_called()