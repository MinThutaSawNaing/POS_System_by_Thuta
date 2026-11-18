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

    def test_named_product_update_can_resolve_id_without_explicit_search_request(self):
        from test_agent_plan_execute import api_response, plan_response, _FakeBody
        decision = self.decision(intent='tools', tools=['update_product_price'], missing=[], message='')
        responses = [_FakeBody(api_response(content=decision.content)), _FakeBody(plan_response(
            'Resolve Cola and update its price', [
                {'step': 1, 'tool': 'search_products', 'args': {'query': 'Cola'}},
                {'step': 2, 'tool': 'update_product_price', 'args': {
                    'product_id': {'$from': 'step1.inventory.0.product_id'}, 'price': 2500}}]))]
        search = mock.Mock(return_value={'inventory': [{'product_id': 7, 'name': 'Cola'}]})
        write = mock.Mock(return_value={'success': True, 'product_id': 7, 'price': '2500.00'})
        with mock.patch('ai_agent.requests.post', side_effect=responses), mock.patch.dict(
                self.orchestrator.agent.tool_functions,
                {'search_products': search, 'update_product_price': write}):
            result = self.orchestrator.process_command('Change the price of Cola to 2500')
        self.assertTrue(result['success'], result)
        search.assert_called_once_with(query='Cola')
        write.assert_called_once_with(product_id=7, price=2500)

    def test_verified_write_routes_by_capability_not_english_keywords(self):
        self.orchestrator._verified_tool_names = {'search_products', 'update_product_price'}
        self.assertTrue(self.orchestrator._should_plan('Cola ကို 2500 ကျပ် သတ်မှတ်ပါ'))
        self.orchestrator._verified_tool_names = None

    def test_preflight_does_not_inherit_business_tool_call_mandate(self):
        from test_agent_plan_execute import api_response, _FakeBody
        response = _FakeBody(api_response(content=self.decision(
            intent='clarify', tools=[], missing=['price'], message='What selling price?').content))
        with mock.patch('ai_agent.requests.post', return_value=response) as api:
            result = self.orchestrator.process_command('Create a product called Cola')
        self.assertIn('selling price', result['message'])
        payload = api.call_args.kwargs['json']
        self.assertNotIn('MUST be answered by calling', payload['messages'][0]['content'])
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertNotIn('tools', payload)

    def test_fenced_valid_decision_does_not_disable_create(self):
        from test_agent_plan_execute import api_response, plan_response, _FakeBody
        decision = self.decision(intent='tools', tools=['register_customer'], missing=[], message='')
        responses = [_FakeBody(api_response(content='```json\n' + decision.content + '\n```')),
                     _FakeBody(plan_response('Register customer', [
                         {'step': 1, 'tool': 'register_customer', 'args': {'name': 'Alice'}}]))]
        write = mock.Mock(return_value={'success': True, 'customer_id': 12, 'name': 'Alice'})
        with mock.patch('ai_agent.requests.post', side_effect=responses), mock.patch.dict(
                self.orchestrator.agent.tool_functions, {'register_customer': write}):
            result = self.orchestrator.process_command('Register a customer named Alice')
        self.assertTrue(result['success'], result)
        write.assert_called_once_with(name='Alice')

    def test_crud_turns_through_real_preflight_and_dispatch(self):
        from test_agent_plan_execute import api_response, plan_response, _FakeBody
        cases = [
            ('Create product Cola with price 2500', 'upsert_product',
             {'name': 'Cola', 'price': 2500}, True),
            ('Show product details for product ID 7', 'get_product_details',
             {'product_id': 7}, False),
            ('Change price of product ID 7 to 2500', 'update_product_price',
             {'product_id': 7, 'price': 2500}, True),
            ('Delete product ID 7', 'delete_product', {'product_id': 7}, True),
        ]
        for command, name, args, mutates in cases:
            with self.subTest(tool=name):
                decision = self.decision(intent='tools', tools=[name], missing=[], message='')
                action = (plan_response('Requested operation', [
                    {'step': 1, 'tool': name, 'args': args}]) if mutates else api_response(
                        tool_calls=[{'id': 'read', 'name': name, 'arguments': args}]))
                responses = [_FakeBody(api_response(content=decision.content)), _FakeBody(action)]
                tool = mock.Mock(return_value={'success': True, 'product_id': 7, 'name': 'Cola'})
                with mock.patch('ai_agent.requests.post', side_effect=responses), mock.patch.dict(
                        self.orchestrator.agent.tool_functions, {name: tool}):
                    result = self.orchestrator.process_command(command)
                self.assertTrue(result['success'], result)
                tool.assert_called_once_with(**args)
                self.assertIsNone(self.orchestrator._verified_tool_names)

    def test_delete_by_name_resolves_target_and_rejects_ambiguous_matches(self):
        from test_agent_plan_execute import api_response, plan_response, _FakeBody
        for ids in ([7], [7, 8]):
            with self.subTest(ids=ids):
                decision = self.decision(intent='tools', tools=['delete_product'], missing=[], message='')
                responses = [_FakeBody(api_response(content=decision.content)), _FakeBody(plan_response(
                    'Find and delete Cola', [
                        {'step': 1, 'tool': 'search_products', 'args': {'query': 'Cola'}},
                        {'step': 2, 'tool': 'delete_product', 'args': {
                            'product_id': {'$from': 'step1.inventory.0.product_id'}}}]))]
                search = mock.Mock(return_value={'inventory': [
                    {'product_id': id, 'name': 'Cola'} for id in ids]})
                delete = mock.Mock(return_value={'success': True, 'product_id': 7})
                with mock.patch('ai_agent.requests.post', side_effect=responses), mock.patch.dict(
                        self.orchestrator.agent.tool_functions,
                        {'search_products': search, 'delete_product': delete}):
                    result = self.orchestrator.process_command('Delete Cola')
                search.assert_called_once_with(query='Cola')
                if len(ids) == 1:
                    self.assertTrue(result['success'], result)
                    delete.assert_called_once_with(product_id=7)
                else:
                    self.assertFalse(result['success'], result)
                    delete.assert_not_called()
                    self.assertIn('several products matched', result['message'])

    def test_write_dependencies_do_not_authorize_unrelated_mutations(self):
        decision = self.decision(intent='tools', tools=['update_product_price'], missing=[], message='')
        with mock.patch.object(self.orchestrator, '_planner_chat', return_value=decision):
            verified = self.orchestrator._verify_tool_intent('Change Cola price to 2500')
        self.assertEqual(set(verified['tools']), {'search_products', 'update_product_price'})
        self.orchestrator._verified_tool_names = set(verified['tools'])
        delete = mock.Mock()
        with mock.patch.dict(self.orchestrator.agent.tool_functions, {'delete_product': delete}):
            results = self.orchestrator._execute_tools_with_context([
                ToolCall('unrelated', 'delete_product', {'product_id': 7})])
        delete.assert_not_called()
        self.assertTrue(results[0]['rejected'])
        self.orchestrator._verified_tool_names = None

    def test_missing_user_value_still_clarifies_and_followup_has_context(self):
        first = self.decision(intent='clarify', tools=[], missing=['price'], message='What selling price?')
        second = self.decision(intent='tools', tools=['upsert_product'], missing=[], message='')
        with mock.patch.object(self.orchestrator, '_planner_chat', return_value=first), mock.patch.object(
                self.orchestrator, '_execute_tools_with_context') as execute:
            self.orchestrator.process_command('Create product Cola')
        execute.assert_not_called()
        with mock.patch.object(self.orchestrator, '_planner_chat', return_value=second) as model:
            decision = self.orchestrator._verify_tool_intent('2500')
        self.assertEqual(decision['intent'], 'tools')
        prompt = model.call_args.kwargs['message']
        self.assertIn('Create product Cola', prompt)
        self.assertIn('What selling price?', prompt)

    def test_truncated_decision_fails_closed_even_with_complete_json(self):
        decision = self.decision(intent='tools', tools=['delete_product'], missing=[], message='')
        decision.finish_reason = 'length'
        with mock.patch.object(self.orchestrator, '_planner_chat', return_value=decision), mock.patch.object(
                self.orchestrator, '_execute_tools_with_context') as execute:
            result = self.orchestrator.process_command('Delete Cola')
        execute.assert_not_called()
        self.assertEqual(result['tool_results'], [])