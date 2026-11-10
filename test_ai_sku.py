"""AI SKU contracts using only an in-memory database, never the POS app."""

from unittest.mock import Mock

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from ai_agent import AIAgent
from ai_tools import AITools, TOOL_SCHEMAS
from agent_orchestrator import AgentOrchestrator, SYSTEM_PROMPT


@pytest.fixture
def catalog():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    db = SQLAlchemy(app)

    class Product(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        branch_id = db.Column(db.Integer)
        name = db.Column(db.String(200))
        sku = db.Column(db.String(100), nullable=True)
        barcode = db.Column(db.String(100))
        category = db.Column(db.String(100))
        category_id = db.Column(db.Integer)
        price = db.Column(db.Float, default=10)
        cost = db.Column(db.Float, default=5)
        stock = db.Column(db.Integer, default=4)
        tax_rate = db.Column(db.Float, default=0)
        reorder_point = db.Column(db.Integer, default=5)
        reorder_quantity = db.Column(db.Integer, default=10)
        reorder_enabled = db.Column(db.Boolean, default=True)
        supplier_prices = ()

    class Category(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        branch_id = db.Column(db.Integer)
        name = db.Column(db.String(100))

    with app.app_context():
        db.create_all()
        db.session.add_all([
            Product(id=1, branch_id=1, name='Widget', sku='AB-123', barcode='987'),
            Product(id=2, branch_id=1, name='AB-123 name decoy', sku='ZZ-999'),
            Product(id=3, branch_id=2, name='Other branch', sku='ab-123'),
            Product(id=4, branch_id=1, name='Unrelated', sku='XX-123'),
        ])
        db.session.commit()
        tools = AITools(db, {'Product': Product, 'Category': Category})
        tools.set_context({'user_id': 1, 'role': 'manager', 'branch_id': 1})
        yield tools, db, Product
        db.session.remove()
        db.engine.dispose()


def test_exact_sku_priority_case_and_branch(catalog):
    tools, _, _ = catalog
    result = tools.search_products(' ab-123 ')
    assert result['matched_by'] == 'sku'
    assert [row['product_id'] for row in result['inventory']] == [1]
    assert result['inventory'][0]['sku'] == 'AB-123'
    assert tools.get_product_details(sku=' ab-123 ')['product_id'] == 1
    assert tools.get_product_details(barcode='987')['sku'] == 'AB-123'
    assert 'error' in tools.get_product_details(barcode='AB-123')
    assert 'error' in tools.get_product_details(sku='AB-123', product_id=2)


def test_no_sku_fragment_or_token_fallback(catalog):
    tools, db, Product = catalog
    db.session.add(Product(branch_id=1, name='XX-124 name decoy', sku='OTHER'))
    db.session.commit()
    assert tools.search_products('XX-124')['inventory'] == []
    assert tools.search_products('ZZ')['inventory'] == []
    assert tools.search_products('XX 123')['inventory'] == []
    assert 'error' in tools.get_product_details(sku='XX-124')


def test_shared_procurement_sku_returns_all_branch_choices(catalog):
    tools, _, _ = catalog
    rows = tools.get_purchase_order_products(' ab-123 ')['products']
    assert {r['branch_id'] for r in rows} == {1, 2}
    assert {r['product_id'] for r in rows} == {1, 3}
    assert all(r['sku'].lower() == 'ab-123' for r in rows)
    assert tools.get_purchase_order_products('AB-124')['products'] == []


def test_ambiguous_unscoped_sku_never_picks_first(catalog):
    tools, _, _ = catalog
    tools.set_context({'user_id': 1, 'role': 'manager'})
    assert 'ambiguous' in tools.get_product_details(sku='ab-123')['error']
    assert tools.get_product_details(sku='ab-123', product_id=3)['product_id'] == 3


def test_sku_create_edit_preserve_clear_and_branch_uniqueness(catalog):
    tools, db, Product = catalog
    result = tools.upsert_product('New', price=12, sku='  NEW-1  ')
    assert result['success'] and result['sku'] == 'NEW-1'
    pid = result['product_id']
    assert tools.update_product(pid, price=13)['sku'] == 'NEW-1'
    assert tools.update_product(pid, sku=' new-2 ')['sku'] == 'new-2'
    assert tools.update_product(pid, sku='  ')['sku'] is None
    assert db.session.get(Product, pid).sku is None
    assert tools.upsert_product('New', price=14, product_id=pid, sku='n')['sku'] == 'n'
    assert tools.upsert_product('New', price=14, product_id=pid)['sku'] == 'n'
    assert tools.upsert_product('New', price=14, product_id=pid, sku='')['sku'] is None
    assert 'already exists' in tools.upsert_product('Duplicate', price=1, sku='ab-123')['error']
    assert tools.update_product(1, sku='ab-123')['success']  # same row, case-only edit
    tools.set_context({'user_id': 1, 'role': 'manager', 'branch_id': 2})
    assert tools.upsert_product('Allowed in B', price=1, sku='ZZ-999')['success']
    assert 'error' in tools.update_product(1, sku='no-cross-branch-write')


@pytest.mark.parametrize('value', [123, False, [], 'x' * 101])
def test_invalid_sku_rejected_before_other_changes(catalog, value):
    tools, db, Product = catalog
    assert 'error' in tools.update_product(1, name='Do not save', sku=value)
    assert 'error' in tools.upsert_product('Do not create', price=1, sku=value)
    db.session.commit()
    assert db.session.get(Product, 1).name == 'Widget'
    assert Product.query.count() == 4


def test_max_length_blank_and_duplicate_update(catalog):
    tools, db, Product = catalog
    assert tools.update_product(1, sku=' ' + 'x' * 100 + ' ')['sku'] == 'x' * 100
    assert 'already exists' in tools.update_product(1, name='Do not save', sku='zz-999')['error']
    db.session.commit()
    assert db.session.get(Product, 1).name == 'Widget'
    assert tools.upsert_product('Blank', price=1, sku='  ')['sku'] is None


def test_sku_visible_in_inventory_low_stock_and_cashier_details(catalog):
    tools, _, _ = catalog
    assert tools.get_inventory_status()['inventory'][0]['sku'] == 'AB-123'
    assert tools.get_low_stock_items()['items'][0]['sku'] == 'AB-123'
    tools.set_context({'user_id': 1, 'role': 'cashier', 'branch_id': 1})
    detail = tools.get_product_details(sku='ab-123')
    assert detail['sku'] == 'AB-123'
    assert 'cost' not in detail and 'supplier_prices' not in detail


def test_legacy_product_without_sku_still_serializes(catalog):
    tools, _, Product = catalog
    legacy = Mock(spec=['id', 'name', 'barcode', 'category', 'price', 'cost',
                        'stock', 'reorder_point', 'reorder_quantity', 'reorder_enabled'])
    legacy.id, legacy.name, legacy.barcode, legacy.category = 99, 'Legacy', '123', None
    legacy.price, legacy.cost, legacy.stock = 10, 5, 4
    legacy.reorder_point, legacy.reorder_quantity, legacy.reorder_enabled = 5, 10, True
    tools._branch_filter = Mock(return_value=Mock(all=Mock(return_value=[legacy])))
    assert tools.get_inventory_status()['inventory'][0]['sku'] is None


def test_sku_prompts_schema_routing_and_formatting():
    agent = AIAgent(api_key='test')
    agent.set_system_prompt('Test')
    assert 'distinct from the physical barcode' in agent.conversation_history[0].content
    assert 'SKU' in SYSTEM_PROMPT
    for tool in ('get_product_details', 'upsert_product', 'update_product'):
        assert 'sku' in TOOL_SCHEMAS[tool]['parameters']['properties']
    orch = AgentOrchestrator(agent, {})
    assert orch._contains_write_intent('clear the sku for widget')
    offered = {t['function']['name'] for t in orch._filter_tools_for_query('find SKU AB-123')}
    assert {'search_products', 'get_product_details'} <= offered
    result = [{'function_name': 'search_products', 'result': {
        'total_products': 1, 'inventory': [{'product_id': 1, 'name': 'Widget', 'sku': 'AB-123', 'barcode': '987'}]}}]
    text = orch._format_tool_results_for_user(result, 'find SKU AB-123')
    assert 'SKU: AB-123' in text and 'Barcode: 987' in text
    agent.chat = Mock()
    empty = [{'function_name': 'search_products', 'result': {
        'inventory': [], 'suggestions': ['Unrelated']}}]
    assert orch._retry_empty_product_search(empty, [], 0, 'find SKU AB-124') == empty
    agent.chat.assert_not_called()