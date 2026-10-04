"""Execution-boundary contracts; no shared database or LLM."""
import json
from unittest.mock import Mock

import pytest
from flask import Flask, session
from sqlalchemy import Column, Integer, String, Boolean, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from ai_agent import AIAgent, ToolCall
from agent_orchestrator import AgentOrchestrator
from ai_tools import AITools, CASHIER_SAFE_TOOLS, TOOL_METADATA, tool_authorization_error

CASHIER = {'role': 'cashier', 'user_id': 7, 'branch_id': 1}
DENIED = sorted(set(TOOL_METADATA) - CASHIER_SAFE_TOOLS)


@pytest.mark.parametrize('name', DENIED)
def test_non_safe_tools_require_manager_before_direct_database_access(name):
    assert TOOL_METADATA[name]['requires_role'] in ('manager', 'boss')
    tools = AITools(None, {})
    tools.set_context(CASHIER)
    assert 'manager' in getattr(tools, name)()['error']


@pytest.mark.parametrize('executor', ['orchestrator', 'agent'])
@pytest.mark.parametrize('name', DENIED + ['unreviewed_future_tool'])
def test_execution_denies_forbidden_registered_function(executor, name):
    obj = AgentOrchestrator(None, {}) if executor == 'orchestrator' else AIAgent()
    obj.set_request_context(CASHIER)
    agent = obj.agent if executor == 'orchestrator' else obj
    function = Mock(return_value={'success': True})
    agent.tool_functions[name] = function
    calls = [ToolCall('deny', name, {'role': 'manager', 'branch_id': 2})]
    results = (obj._execute_tools_with_context(calls) if executor == 'orchestrator'
               else obj.execute_tool_calls(calls))
    assert results[0]['error']
    function.assert_not_called()


@pytest.mark.parametrize('branch', [None, 0, -1, '', '1', True])
@pytest.mark.parametrize('name', sorted(CASHIER_SAFE_TOOLS))
def test_invalid_branch_fails_closed_before_database(name, branch):
    tools = AITools(None, {})
    tools.set_context(dict(CASHIER, branch_id=branch))
    assert 'trusted active branch' in getattr(tools, name)()['error']


@pytest.mark.parametrize('context', [{'user_id': 7, 'branch_id': 1},
                                     {'role': 'unknown', 'branch_id': 1},
                                     dict(CASHIER, user_id=None)])
def test_incomplete_authenticated_context_cannot_use_legacy_mode(context):
    assert tool_authorization_error('get_inventory_status', context)


def test_empty_context_is_legacy_only_outside_http_requests():
    assert tool_authorization_error('get_sales_summary', {}) is None
    app = Flask(__name__)
    app.secret_key = 'test'
    with app.test_request_context('/'):
        assert tool_authorization_error('get_inventory_status', {})
        session.update(CASHIER)
        assert tool_authorization_error('get_sales_summary', {})
        session.pop('branch_id')
        assert tool_authorization_error('get_inventory_status', {})


@pytest.mark.parametrize('role', ['manager', 'boss'])
def test_manager_preserves_full_catalog_and_execution(role):
    orch = AgentOrchestrator(None, {})
    orch.set_request_context(dict(CASHIER, role=role))
    assert set(TOOL_METADATA) <= set(orch._get_tool_registry())
    function = Mock(return_value={'cost': '12.00', 'profit': '4.00'})
    orch.agent.tool_functions['get_sales_summary'] = function
    result = orch._execute_tools_with_context([ToolCall('ok', 'get_sales_summary', {})])
    assert result[0]['result'] == {'cost': '12.00', 'profit': '4.00'}
    function.assert_called_once()


def test_cashier_offered_schemas_and_planning_catalog_are_safe():
    orch = AgentOrchestrator(None, {})
    orch.set_request_context(CASHIER)
    assert set(orch._get_tool_registry()) == CASHIER_SAFE_TOOLS
    offered = orch._filter_tools_for_query('show all business information')
    assert {tool['function']['name'] for tool in offered} == CASHIER_SAFE_TOOLS


@pytest.fixture
def inventory_tools():
    Base = declarative_base()

    class Product(Base):
        __tablename__ = 'product'
        id = Column(Integer, primary_key=True)
        branch_id = Column(Integer)
        name = Column(String)
        barcode = Column(String)
        category = Column(String)
        stock = Column(Integer, default=2)
        reorder_point = Column(Integer, default=5)
        reorder_quantity = Column(Integer, default=10)
        reorder_enabled = Column(Boolean, default=True)
        price = Column(Integer, default=20)
        cost = Column(Integer, default=12)
        tax_rate = Column(Integer, default=0)

        @property
        def supplier_prices(self):
            raise AssertionError('Cashier must not query supplier pricing')

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db_session = sessionmaker(bind=engine)()
    Product.query = db_session.query(Product)
    db_session.add_all([Product(id=1, branch_id=1, name='Local', barcode='one'),
                        Product(id=2, branch_id=2, name='Other', barcode='two')])
    db_session.commit()
    tools = AITools(None, {'Product': Product})
    tools.set_context(CASHIER)
    yield tools
    db_session.close()
    engine.dispose()


@pytest.mark.parametrize('name,args', [
    ('get_inventory_status', {}), ('get_low_stock_items', {}),
    ('search_products', {'query': 'o'}), ('get_product_details', {'product_id': 1}),
])
def test_inventory_is_branch_scoped_and_minimized(inventory_tools, name, args):
    result = getattr(inventory_tools, name)(**args)
    serialized = json.dumps(result)
    assert 'Local' in serialized
    assert 'Other' not in serialized
    for forbidden in ('cost', 'profit', 'supplier', 'agreed_price'):
        assert forbidden not in serialized
    assert result['branch_id'] == 1


@pytest.mark.parametrize('args', [{'product_id': 2}, {'barcode': 'two'}])
def test_other_branch_product_not_found(inventory_tools, args):
    assert inventory_tools.get_product_details(**args)['error'] == 'Product not found'



@pytest.mark.parametrize('executor', ['orchestrator', 'agent'])
def test_minimization_precedes_history_and_model_payload(executor):
    obj = AgentOrchestrator(None, {}) if executor == 'orchestrator' else AIAgent()
    obj.set_request_context(CASHIER)
    agent = obj.agent if executor == 'orchestrator' else obj
    agent.tool_functions['get_inventory_status'] = lambda: {
        'inventory': [{'name': 'Local', 'price': '20.00', 'cost': '12.00',
                       'supplier_prices': [{'agreed_price': '10.00'}],
                       'future_financial_field': 'secret'}]}
    calls = [ToolCall('safe', 'get_inventory_status', {})]
    result = (obj._execute_tools_with_context(calls) if executor == 'orchestrator'
              else obj.execute_tool_calls(calls))
    assert result[0]['result'] == {'inventory': [{'name': 'Local', 'price': '20.00'}]}
    payload = json.dumps(agent._build_messages_payload())
    assert 'secret' not in payload
    assert 'cost' not in payload


def test_branch_only_legacy_context_does_not_bypass_http_cashier_policy():
    tools = AITools(None, {})
    tools.set_context({'branch_id': 1})
    app = Flask(__name__)
    app.secret_key = 'test'
    with app.test_request_context('/'):
        session.update(CASHIER)
        assert 'manager' in tools.get_sales_summary()['error']


@pytest.mark.parametrize('context', [{'branch_id': None}, {'branch_id': 2},
                                     dict(CASHIER, branch_id=2, role='manager')])
def test_http_scope_wins_for_authorization_and_inventory_queries(inventory_tools, context):
    app = Flask(__name__)
    app.secret_key = 'test'
    with app.test_request_context('/'):
        session.update(CASHIER)
        inventory_tools.set_context(context)
        result = inventory_tools.get_inventory_status()
        assert result['branch_id'] == 1
        assert [p['name'] for p in result['inventory']] == ['Local']
        assert 'manager' in inventory_tools.get_sales_summary()['error']


def test_orchestrator_captures_missing_branch_before_helper_defaults_it():
    from agent_orchestrator import get_orchestrator, reset_orchestrator
    app = Flask(__name__)
    app.secret_key = 'test'
    with app.test_request_context('/'):
        session.update(user_id=7, role='cashier')
        orch = get_orchestrator(Mock(), {}, conversation_id='missing-branch-test')
        # Simulate get_current_branch_id's default selection after factory lookup.
        session['branch_id'] = 1
        orch.set_request_context(CASHIER)
        assert tool_authorization_error('get_inventory_status', orch.request_context)
    reset_orchestrator()

