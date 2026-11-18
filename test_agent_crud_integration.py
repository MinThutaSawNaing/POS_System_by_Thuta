"""Real CRUD tools behind semantic routing, using a disposable database."""
import json
import unittest
from unittest.mock import patch

from agent_orchestrator import AgentOrchestrator
from test_agent_plan_execute import api_response, plan_response, _FakeBody
import test_ai_shared_workflows as shared


class AgentCRUDIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(shared.SharedAIWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(shared.SharedAIWorkflowTests.tearDownClass.__func__)
    setUp = shared.SharedAIWorkflowTests.setUp
    tearDown = shared.SharedAIWorkflowTests.tearDown

    def test_create_read_update_delete_through_semantic_preflight(self):
        m = self.module
        orch = AgentOrchestrator(m.db, m.AI_MODELS, app=self.app)
        orch.set_request_context({'user_id': self.user, 'role': 'manager', 'branch_id': self.a})

        def turn(command, name, steps=None, arguments=None):
            decision = {'intent': 'tools', 'tools': [name], 'missing': [], 'message': ''}
            action = plan_response(command, steps) if steps else api_response(tool_calls=[{
                'id': 'read', 'name': name, 'arguments': arguments or {}}])
            responses = [_FakeBody(api_response(content=json.dumps(decision))), _FakeBody(action)]
            with patch('ai_agent.requests.post', side_effect=responses):
                result = orch.process_command(command)
            self.assertTrue(result['success'], result)
            self.assertIsNone(orch._verified_tool_names)
            return result

        turn('Create product Regression Cola priced 2500 with barcode CRUD-TEST', 'upsert_product', [
            {'step': 1, 'tool': 'upsert_product', 'args': {
                'name': 'Regression Cola', 'price': 2500, 'barcode': 'CRUD-TEST'}}])
        product = m.Product.query.filter_by(barcode='CRUD-TEST', branch_id=self.a).one()
        product_id = product.id
        self.assertEqual(float(product.price), 2500)

        result = turn('Show product with barcode CRUD-TEST', 'get_product_details',
                      arguments={'barcode': 'CRUD-TEST'})
        self.assertIn('Regression Cola', result['message'])

        turn('Change price of Regression Cola to 3000', 'update_product_price', [
            {'step': 1, 'tool': 'search_products', 'args': {'query': 'Regression Cola'}},
            {'step': 2, 'tool': 'update_product_price', 'args': {
                'product_id': {'$from': 'step1.inventory.0.product_id'}, 'price': 3000}}])
        m.db.session.expire_all()
        self.assertEqual(float(m.db.session.get(m.Product, product_id).price), 3000)

        turn('Delete product Regression Cola', 'delete_product', [
            {'step': 1, 'tool': 'search_products', 'args': {'query': 'Regression Cola'}},
            {'step': 2, 'tool': 'delete_product', 'args': {
                'product_id': {'$from': 'step1.inventory.0.product_id'}}}])
        # Tools run in their own app-context/scoped session; discard the
        # outer test session's cached object before checking persisted state.
        m.db.session.expire_all()
        self.assertIsNone(m.db.session.get(m.Product, product_id))