"""Account resolution belongs to the trusted caller, never the tool container."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, session

from agent_orchestrator import AgentOrchestrator
from ai_tools import AITools, tool_authorization_error


@pytest.mark.parametrize('role', ['manager', 'boss', 'cashier'])
def test_orchestrator_resolves_legacy_identity_before_distributing_context(role):
    account = Mock()
    account.query.filter_by.return_value.first.return_value = SimpleNamespace(role=role)
    orch = AgentOrchestrator(None, {'User': account})
    original = {'user_id': 7, 'branch_id': 1}
    orch.set_request_context(original)
    expected = dict(original, role=role)
    assert orch.request_context == expected
    assert orch.ai_tools.context == expected
    assert orch.agent.request_context == expected
    assert 'role' not in original
    account.query.filter_by.assert_called_once_with(id=7)
    if role == 'cashier':
        assert tool_authorization_error('get_sales_summary', orch.request_context)


def test_direct_tool_context_does_not_resolve_accounts():
    account = Mock()
    tools = AITools(None, {'User': account})
    tools.set_context({'user_id': 7, 'branch_id': 1})
    account.query.filter_by.assert_not_called()
    assert tool_authorization_error('get_sales_summary', tools.context)
    tools.set_context()
    assert tool_authorization_error('get_sales_summary', tools.context) is None


@pytest.mark.parametrize('context', [{}, {'branch_id': 1},
                                     {'user_id': 7, 'branch_id': 1, 'role': 'cashier'},
                                     {'user_id': 7, 'branch_id': 1, 'role': None}])
def test_orchestrator_does_not_infer_without_identity_or_overwrite_explicit_role(context):
    account = Mock()
    orch = AgentOrchestrator(None, {'User': account})
    orch.set_request_context(context)
    assert orch.request_context == context
    account.query.filter_by.assert_not_called()


@pytest.mark.parametrize('models', [{}, {'User': Mock()}])
def test_unresolved_identity_fails_closed(models):
    if models:
        models['User'].query.filter_by.return_value.first.return_value = None
    orch = AgentOrchestrator(None, models)
    orch.set_request_context({'user_id': 7, 'branch_id': 1})
    assert tool_authorization_error('get_sales_summary', orch.request_context)


def test_http_session_wins_without_legacy_account_lookup():
    account = Mock()
    orch = AgentOrchestrator(None, {'User': account})
    app = Flask(__name__)
    app.secret_key = 'test'
    with app.test_request_context('/'):
        session.update(user_id=7, role='cashier', branch_id=1)
        orch.set_request_context({'user_id': 8, 'branch_id': 2})
        account.query.filter_by.assert_not_called()
        assert orch.request_context == {'user_id': 7, 'role': 'cashier', 'branch_id': 1}
        assert tool_authorization_error('get_sales_summary', orch.request_context)
