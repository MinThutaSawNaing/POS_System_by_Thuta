"""Account resolution belongs to the trusted caller, never the tool container.

The tools themselves have no permission layer any more, so these tests only pin
how request scope is derived and distributed: an identity may be resolved by the
orchestrator, an HTTP session always wins, and a tool container never queries
accounts on its own.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, session

from agent_orchestrator import AgentOrchestrator
from ai_tools import AITools


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


def test_direct_tool_context_does_not_resolve_accounts():
    account = Mock()
    tools = AITools(None, {'User': account})
    tools.set_context({'user_id': 7, 'branch_id': 1})
    account.query.filter_by.assert_not_called()
    tools.set_context()
    assert tools.context == {}


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
def test_unresolved_identity_keeps_the_caller_context(models):
    if models:
        models['User'].query.filter_by.return_value.first.return_value = None
    orch = AgentOrchestrator(None, models)
    orch.set_request_context({'user_id': 7, 'branch_id': 1})
    assert orch.request_context == {'user_id': 7, 'branch_id': 1}


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
