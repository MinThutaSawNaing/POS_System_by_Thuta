"""Tool execution boundary contracts; no shared database or LLM.

The per-tool permission/approval model has been removed: the agent executes
every registered tool. Two things still matter here:

* nothing in the registry re-introduces a role or autonomy gate, and
* the branch scope still comes from trusted context, so a branch-less context
  can never widen a query across branches.

Cashier accounts cannot reach the assistant at all; that is covered at the
HTTP boundary by ``test_cashier_permissions.py``.
"""

from unittest.mock import Mock

import pytest
from sqlalchemy import Boolean, Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from ai_agent import AIAgent, ToolCall
from agent_orchestrator import AgentOrchestrator
from ai_tools import AITools, TOOL_METADATA

CONTEXT = {'role': 'cashier', 'user_id': 7, 'branch_id': 1}
WRITE_TOOLS = sorted(name for name, meta in TOOL_METADATA.items() if meta['mutates'])


def test_registry_carries_no_permission_metadata():
    for name, meta in TOOL_METADATA.items():
        if meta['category'] == 'deliveries':
            assert meta['requires_role'] == ['manager', 'boss'], name
            assert meta['autonomy'] == ('approval' if name == 'delete_delivery_record' else 'auto'), name
        else:
            assert 'requires_role' not in meta, name
            assert 'autonomy' not in meta, name


@pytest.mark.parametrize('executor', ['orchestrator', 'agent'])
@pytest.mark.parametrize('name,args', [
    ('get_sales_summary', {}),
    ('delete_product', {'product_id': 7}),
])
def test_execution_has_no_permission_gate(executor, name, args):
    """A registered function runs for any authenticated scope, even a cashier."""
    obj = AgentOrchestrator(None, {}) if executor == 'orchestrator' else AIAgent()
    obj.set_request_context(CONTEXT)
    agent = obj.agent if executor == 'orchestrator' else obj
    function = Mock(return_value={'success': True})
    agent.tool_functions[name] = function
    calls = [ToolCall('run', name, dict(args))]
    results = (obj._execute_tools_with_context(calls) if executor == 'orchestrator'
               else obj.execute_tool_calls(calls))
    assert results[0]['result'] == {'success': True}
    function.assert_called_once()


def test_write_tools_are_registered_and_offered():
    """Writes are visible to the planner and the single-shot chat path."""
    assert {'delete_product', 'update_product_price', 'create_purchase_order'} <= set(WRITE_TOOLS)
    orch = AgentOrchestrator(None, {})
    orch.set_request_context(CONTEXT)
    assert set(orch._get_tool_registry()) == set(TOOL_METADATA)
    offered = {tool['function']['name']
               for tool in orch._filter_tools_for_query('show all business information')}
    assert set(TOOL_METADATA) <= offered


@pytest.mark.parametrize('tool,args', [
    ('delete_product', {'product_id': 7}),
    ('create_purchase_order', {'supplier_id': 1,
                               'items': [{'product_id': 5, 'quantity': 1}]}),
])
def test_plan_executes_writes_without_approval(tool, args):
    """A mutating step runs in PHASE B instead of becoming a proposal."""
    orch = AgentOrchestrator(None, {})
    orch.set_request_context(CONTEXT)
    function = Mock(return_value={'success': True})
    orch.agent.tool_functions[tool] = function
    step_results, executed_writes = orch._execute_plan(
        {'description': 'apply', 'steps': [{'step': 1, 'tool': tool, 'args': args}]})
    assert step_results[0]['status'] == 'ok'
    assert executed_writes == [1]
    assert 'executed_by' not in step_results[0]
    function.assert_called_once()


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
            return []

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db_session = sessionmaker(bind=engine)()
    Product.query = db_session.query(Product)
    db_session.add_all([Product(id=1, branch_id=1, name='Local', barcode='one'),
                        Product(id=2, branch_id=2, name='Other', barcode='two')])
    db_session.commit()
    tools = AITools(None, {'Product': Product})
    tools.set_context(CONTEXT)
    yield tools
    db_session.close()
    engine.dispose()


@pytest.mark.parametrize('name,args', [
    ('get_inventory_status', {}), ('get_low_stock_items', {}),
    ('search_products', {'query': 'o'}), ('get_product_details', {'product_id': 1}),
])
def test_inventory_is_branch_scoped(inventory_tools, name, args):
    result = getattr(inventory_tools, name)(**args)
    assert result['branch_id'] == 1
    names = [row.get('name') for row in result.get('inventory', [])
             if isinstance(row, dict)]
    if names:
        assert 'Other' not in names


@pytest.mark.parametrize('args', [{'product_id': 2}, {'barcode': 'two'}])
def test_other_branch_product_not_found(inventory_tools, args):
    assert inventory_tools.get_product_details(**args)['error'] == 'Product not found'


def test_http_scope_wins_over_a_stale_caller_context(inventory_tools):
    from flask import Flask, session

    app = Flask(__name__)
    app.secret_key = 'test'
    with app.test_request_context('/'):
        session.update(CONTEXT)
        inventory_tools.set_context({'branch_id': 2})
        result = inventory_tools.get_inventory_status()
        assert result['branch_id'] == 1
        assert [p['name'] for p in result['inventory']] == ['Local']
