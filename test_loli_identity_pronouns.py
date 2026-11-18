"""Loli's self-reference policy must reach both model response paths."""
import json
import unittest
from unittest import mock

from agent_orchestrator import AgentOrchestrator, INTENT_SYSTEM_PROMPT, SYSTEM_PROMPT
from test_agent_plan_execute import _FakeBody, api_response


class LoliPronounTests(unittest.TestCase):
    def assert_pronoun_policy(self, prompt):
        self.assertIn('she/her', prompt)
        self.assertIn('herself', prompt)
        self.assertIn('not he/him', prompt)
        self.assertIn('other people', prompt)

    def test_both_prompt_templates_specify_feminine_self_reference(self):
        for prompt in (SYSTEM_PROMPT, INTENT_SYSTEM_PROMPT):
            with self.subTest(prompt=prompt[:40]):
                self.assert_pronoun_policy(prompt)

    def test_applied_execution_prompt_keeps_policy(self):
        orchestrator = AgentOrchestrator(None, {})
        prompt = next(m.content for m in orchestrator.agent.conversation_history if m.role == 'system')
        self.assert_pronoun_policy(prompt)
        self.assertIn('Current Date:', prompt)

    def test_actual_preflight_payload_keeps_policy_without_enabling_tools(self):
        orchestrator = AgentOrchestrator(None, {})
        decision = {'intent': 'chat', 'tools': [], 'missing': [],
                    'message': 'I use she/her pronouns.'}
        response = _FakeBody(api_response(content=json.dumps(decision)))
        with mock.patch('ai_agent.requests.post', return_value=response) as api:
            result = orchestrator.process_command('What pronouns do you use?')
        self.assertEqual(result['message'], decision['message'])
        payload = api.call_args.kwargs['json']
        self.assert_pronoun_policy(payload['messages'][0]['content'])
        self.assertNotIn('tools', payload)

    def test_summary_request_transmits_execution_identity_policy(self):
        orchestrator = AgentOrchestrator(None, {})
        response = _FakeBody(api_response(content='The requested checks are complete.'))
        steps = [
            {'step': 1, 'tool': 'get_inventory_status', 'status': 'ok',
             'result': {'total_products': 0, 'inventory': []}},
            {'step': 2, 'tool': 'get_low_stock_items', 'status': 'ok',
             'result': {'items': []}},
        ]
        with mock.patch('ai_agent.requests.post', return_value=response) as api:
            orchestrator._generate_plan_summary('Summarize stock and low stock', {}, steps)
        payload = api.call_args.kwargs['json']
        self.assert_pronoun_policy(payload['messages'][0]['content'])
        self.assertNotIn('tools', payload)