"""First-turn semantic routing, without network or database access."""
import json
from unittest import mock

from ai_agent import ChatResponse, ToolCall
from test_agent_smarts import SmartsTestBase


class IntentPreflightTests(SmartsTestBase):
    def setUp(self):
        super().setUp()
        self.intent_patch.stop()

    def decision(self, **values):
        return ChatResponse(content=json.dumps(values))

    def test_ambiguous_count_clarifies_before_any_dispatch(self):
        with mock.patch.object(self.orchestrator, '_planner_chat', return_value=self.decision(
                intent='clarify', message='Do you mean units in stock or units sold today?',
                tools=[], missing=['metric'])), mock.patch.object(
                self.orchestrator.agent, 'chat') as chat, mock.patch.object(
                self.orchestrator, '_execute_tools_with_context') as execute:
            result = self.orchestrator.process_command('hello, how many Cola today?')
        chat.assert_not_called()
        execute.assert_not_called()
        self.assertIn('stock or units sold', result['message'])

    def test_clear_request_offers_only_semantically_selected_tool(self):
        reply = ChatResponse(content='', tool_calls=[ToolCall('read', 'get_inventory_status', {})])
        with mock.patch.object(self.orchestrator, '_planner_chat', return_value=self.decision(
                intent='tools', tools=['get_sales_summary'], missing=[], message='')), \
                mock.patch.object(self.orchestrator.agent, 'chat', return_value=reply) as chat, \
                mock.patch.dict(self.orchestrator.agent.tool_functions,
                                {'get_inventory_status': mock.Mock()}) as functions:
            self.orchestrator.process_command('How many sales today?')
            functions['get_inventory_status'].assert_not_called()
        names = [s['function']['name'] for s in chat.call_args.kwargs['tools_override']]
        self.assertEqual(names, ['get_sales_summary'])

    def test_invalid_or_unavailable_verification_fails_closed(self):
        for response in (ChatResponse(content='', error='offline'), ChatResponse(content='not json'),
                         self.decision(intent='tools', tools=['made_up'], missing=[], message=''),
                         self.decision(intent='tools', tools=['get_inventory_status'],
                                       missing=['product or metric'], message='Which metric?')):
            with self.subTest(response=response), mock.patch.object(
                    self.orchestrator, '_planner_chat', return_value=response), mock.patch.object(
                    self.orchestrator.agent, 'chat') as chat:
                result = self.orchestrator.process_command('hello, how many X today?')
            chat.assert_not_called()
            self.assertEqual(result['tool_results'], [])

    def test_chat_does_not_offer_tools(self):
        with mock.patch.object(self.orchestrator, '_planner_chat', return_value=self.decision(
                intent='chat', tools=[], missing=[], message='Hello!')), mock.patch.object(
                self.orchestrator.agent, 'chat') as chat:
            result = self.orchestrator.process_command('Hello!')
        chat.assert_not_called()
        self.assertEqual(result['message'], 'Hello!')

    def test_preflight_is_tool_free_and_has_full_catalog_and_context(self):
        with mock.patch.object(self.orchestrator, '_planner_chat', return_value=self.decision(
                intent='clarify', tools=[], missing=['metric'], message='Which metric?')) as model:
            self.orchestrator.process_command('hello, how many X today?')
        self.assertIsNone(model.call_args.kwargs['tools'])
        prompt = model.call_args.kwargs['message']
        self.assertIn('get_sales_summary', prompt)
        self.assertIn('get_inventory_status', prompt)
        self.assertIn('hello, how many X today?', prompt)

    def test_required_parameters_rejected_before_dispatch(self):
        for arguments in ({}, {'query': ''}, {'query': '   '}, {'query': None}):
            with self.subTest(arguments=arguments), mock.patch.dict(
                    self.orchestrator.agent.tool_functions, {'search_products': mock.Mock()}) as functions:
                self.orchestrator._verified_tool_names = {'search_products'}
                results = self.orchestrator._execute_tools_with_context(
                    [ToolCall('search', 'search_products', arguments)])
                functions['search_products'].assert_not_called()
                self.assertTrue(results[0]['rejected'])
        self.orchestrator._verified_tool_names = None

    def test_legacy_and_fallback_cannot_escape_verified_intent(self):
        from agent_orchestrator import TaskStep
        self.orchestrator._verified_tool_names = {'get_sales_summary'}
        with mock.patch.object(self.orchestrator.ai_tools, 'get_inventory_status') as inventory:
            self.assertIsNone(self.orchestrator._run_fallback_tool('get_inventory_status'))
            with self.assertRaises(ValueError):
                self.orchestrator._execute_single_step(TaskStep('get_inventory_status', 'wrong'), {})
        inventory.assert_not_called()
        self.orchestrator._verified_tool_names = None

    def test_first_clear_turn_executes_sales_not_inventory(self):
        from test_agent_plan_execute import api_response, _FakeBody
        decision = self.decision(intent='tools', tools=['get_sales_summary'], missing=[], message='')
        responses = [_FakeBody(api_response(content=decision.content)), _FakeBody(api_response(
            tool_calls=[{'id': 'sales', 'name': 'get_sales_summary', 'arguments': {}}]))]
        sales = mock.Mock(return_value={'total_sales': 3})
        with mock.patch('ai_agent.requests.post', side_effect=responses) as api, mock.patch.dict(
                self.orchestrator.agent.tool_functions, {'get_sales_summary': sales}), mock.patch.object(
                self.orchestrator, '_answer_tool_results', return_value='3 sales today.'):
            result = self.orchestrator.process_command('How many sales today?')
        self.assertEqual(result['message'], '3 sales today.')
        sales.assert_called_once_with()
        self.assertNotIn('tools', api.call_args_list[0].kwargs['json'])
        self.assertEqual([t['function']['name'] for t in api.call_args_list[1].kwargs['json']['tools']],
                         ['get_sales_summary'])
        self.assertIsNone(self.orchestrator._verified_tool_names)