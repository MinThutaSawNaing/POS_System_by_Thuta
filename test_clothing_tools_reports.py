"""Disposable in-memory tools/report contracts; never import the deployed app."""

import pytest
from flask import Flask, session
from flask_sqlalchemy import SQLAlchemy
from unittest.mock import patch

from ai_tools import AITools, TOOL_METADATA, TOOL_SCHEMAS, get_all_tools
import reports


@pytest.fixture
def inventory():
    app = Flask(__name__)
    app.config.update(SECRET_KEY='test-only', SQLALCHEMY_DATABASE_URI='sqlite://',
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db = SQLAlchemy(app)

    class Product(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        catalog_id = db.Column(db.String)
        branch_id = db.Column(db.Integer)
        name = db.Column(db.String)
        barcode = db.Column(db.String)
        stock = db.Column(db.Integer)
        price = db.Column(db.Float, default=12.5)
        cost = db.Column(db.Float, default=4)
        tax_rate = db.Column(db.Float, default=0)
        category = db.Column(db.String)
        category_id = db.Column(db.Integer)
        reorder_enabled = db.Column(db.Boolean, default=True)
        reorder_point = db.Column(db.Integer, default=3)
        reorder_quantity = db.Column(db.Integer, default=50)

    class ClothingStyle(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String)
        code = db.Column(db.String)
        archived = db.Column(db.Boolean, default=False)

    class ClothingVariant(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        catalog_id = db.Column(db.String, unique=True)
        style_id = db.Column(db.Integer, db.ForeignKey('clothing_style.id'))
        size = db.Column(db.String)
        color = db.Column(db.String)
        archived = db.Column(db.Boolean, default=False)
        style = db.relationship(ClothingStyle)

    class Branch(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        is_active = db.Column(db.Boolean, default=True)

    class WarehouseInventory(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        product_id = db.Column(db.Integer)
        quantity = db.Column(db.Integer)
        received_date = db.Column(db.DateTime)
        batch_number = db.Column(db.String)

    class Sale(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        transaction_id = db.Column(db.String)
        branch_id = db.Column(db.Integer)

    class SaleItem(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        sale_id = db.Column(db.Integer)
        product_id = db.Column(db.Integer)
        quantity = db.Column(db.Integer)
        price = db.Column(db.Float, default=12.5)
        tax = db.Column(db.Float, default=0)

    models = dict(Product=Product, ClothingStyle=ClothingStyle, ClothingVariant=ClothingVariant,
                  Branch=Branch, WarehouseInventory=WarehouseInventory, Sale=Sale, SaleItem=SaleItem)
    with app.app_context():
        db.create_all()
        db.session.add_all([ClothingStyle(id=1, name='Linen Shirt', code='LN'),
                            ClothingStyle(id=2, name='100% Cotton', code='CT'),
                            ClothingStyle(id=3, name='Old', archived=True)])
        for variant_id, catalog, style_id, size, color, archived in [
            (1, 'small', 1, 'S', 'Blue', False), (2, 'large', 1, 'L', 'Red', False),
            (3, 'old', 1, 'M', 'Blue', True), (4, 'cotton', 2, 'S', 'White', False),
            (5, 'oldstyle', 3, 'S', 'Blue', False)]:
            db.session.add(ClothingVariant(id=variant_id, catalog_id=catalog, style_id=style_id,
                                          size=size, color=color, archived=archived))
        for product_id, catalog, branch, stock in [(1, 'small', 1, 2), (2, 'large', 1, 12),
                (3, 'small', 2, 99), (4, 'old', 1, 1), (5, 'cotton', 1, 0),
                (6, 'ordinary', 1, 50), (7, 'oldstyle', 1, 2)]:
            db.session.add(Product(id=product_id, catalog_id=catalog, branch_id=branch,
                                   name=catalog, barcode=catalog, stock=stock))
        db.session.commit()
        tools = AITools(db, models)
        tools.set_context(dict(branch_id=1, user_id=10, role='cashier'))
        yield app, db, models, tools
        db.session.remove()
        db.drop_all()


def test_read_tool_metadata_and_scope(inventory):
    _, _, _, tools = inventory
    meta = TOOL_METADATA['get_clothing_inventory']
    assert meta['mutates'] is False
    assert meta['requires_role'] is None
    assert 'get_clothing_inventory' in get_all_tools(read_only=True)
    assert 'branch_id' not in TOOL_SCHEMAS['get_clothing_inventory']['parameters']['properties']
    result = tools.get_clothing_inventory()
    assert result['branch_id'] == 1
    assert result['total_products'] == 3
    assert {row['product_id'] for row in result['inventory']} == {1, 2, 5}
    assert result['inventory'][0]['size'] == 'S'
    assert result['inventory'][0]['style_name'] == 'Linen Shirt'
    assert result['inventory'][0]['price'] == '12.50'
    assert all('cost' not in row and 'stock_age' not in row for row in result['inventory'])


def test_read_tool_filters_real_stock_and_limit(inventory):
    _, _, _, tools = inventory
    result = tools.get_clothing_inventory(style='ln', size='s', color='blue', low_stock_only=True)
    assert [row['product_id'] for row in result['inventory']] == [1]
    assert result['inventory'][0]['current_stock'] == 2
    assert result['inventory'][0]['status'] == 'low_stock'
    assert tools.get_clothing_inventory(style_id=1)['total_products'] == 2
    assert tools.get_clothing_inventory(style='%')['total_products'] == 1
    assert tools.get_clothing_inventory(low_stock_only=True)['total_products'] == 2
    result = tools.get_clothing_inventory(limit=1)
    assert result['total_products'] == 3 and result['truncated'] is True
    assert tools.get_clothing_inventory(color='missing')['inventory'] == []


@pytest.mark.parametrize('arguments', [dict(style_id=True), dict(limit=0), dict(limit='2'),
    dict(low_stock_only='true'), dict(size=''), dict(color=[]), dict(style='x' * 101)])
def test_read_tool_rejects_invalid_filters(inventory, arguments):
    assert 'error' in inventory[3].get_clothing_inventory(**arguments)


def test_current_request_session_wins_and_missing_scope_fails(inventory):
    app, _, _, tools = inventory
    with app.test_request_context('/'):
        session.update(branch_id=2, user_id=20, role='cashier')
        result = tools.get_clothing_inventory()
        assert result['branch_id'] == 2
        assert result['inventory'][0]['current_stock'] == 99
    tools.set_context({})
    assert 'error' in tools.get_clothing_inventory()
    tools.set_context(dict(branch_id=1, user_id=10, role='unknown'))
    assert 'error' in tools.get_clothing_inventory()


def test_old_product_writes_cannot_change_clothing_identity(inventory):
    _, db, models, tools = inventory
    tools.set_context(dict(branch_id=1, user_id=10, role='manager'))
    assert 'immutable' in tools.upsert_product('shirt', 12, product_id=1, stock=2,
                                              barcode='changed')['error']
    assert 'overwritten' in tools.upsert_product('shirt', 12, product_id=1, stock=0)['error']
    tools.set_context(dict(branch_id=2, user_id=10, role='manager'))
    assert 'reserved' in tools.upsert_product('new', 12, barcode='cotton')['error']
    assert 'archive' in tools.delete_product(3, confirm=True, cascade=True)['error']
    assert db.session.get(models['Product'], 3).stock == 99


def test_clothing_price_update_can_omit_stock(inventory):
    _, db, models, tools = inventory
    tools.set_context(dict(branch_id=1, user_id=10, role='manager'))
    before = db.session.get(models['Product'], 1).stock
    result = tools.upsert_product('Linen Shirt', 15, product_id=1)
    assert result['success'], result
    product = db.session.get(models['Product'], 1)
    assert product.stock == before == 2
    assert product.price == 15


def test_transfer_rejects_cross_catalog_barcode_before_stock_mutation(inventory):
    _, db, models, tools = inventory
    tools.set_context(dict(branch_id=1, user_id=10, role='manager'))
    target = db.session.get(models['Product'], 3)
    target.catalog_id = 'unrelated'
    db.session.add(models['Branch'](id=2))
    db.session.add(models['WarehouseInventory'](id=1, product_id=1, quantity=10))
    db.session.commit()
    result = tools.create_warehouse_transfer(1, 3, target_branch_id=2)
    assert 'different catalog' in result['error']
    assert db.session.get(models['WarehouseInventory'], 1).quantity == 10
    assert db.session.get(models['Product'], 3).stock == 99


def test_clothing_return_directs_shared_workflow_without_stock_mutation(inventory):
    _, db, models, tools = inventory
    tools.set_context(dict(branch_id=1, user_id=10, role='manager'))
    db.session.add(models['Sale'](id=1, transaction_id='sale1', branch_id=1))
    db.session.add(models['SaleItem'](id=1, sale_id=1, product_id=1, quantity=1))
    db.session.commit()
    with patch.object(tools, '_returned_qty_map', return_value={}):
        result = tools.process_return_exchange('sale1', [dict(sale_item_id=1, quantity=1)])
    assert 'Returns & Exchanges' in result['error']
    assert db.session.get(models['Product'], 1).stock == 2


@pytest.mark.parametrize('builder,base', [
    (reports.build_product_catalog_report, dict(product_name='Shirt', stock=2, price=12)),
    (reports.build_warehouse_stock_report, dict(product_name='Shirt', product_id=1, quantity=2, unit_cost=4)),
])
def test_optional_report_columns_preserve_totals(builder, base):
    ordinary = builder([base])
    detail = dict(style_name='Linen Shirt', size='S', color='Blue', branch_name='Downtown')
    clothing = builder([{**base, **detail}, base])
    assert [entry['key'] for entry in clothing['columns']][-4:] == list(detail)
    assert all(key not in {entry['key'] for entry in ordinary['columns']} for key in detail)
    assert clothing['rows'][0].items() >= detail.items()
    assert clothing['totals'] == {key: value * 2 for key, value in ordinary['totals'].items()}
    assert reports.build_report_pdf(clothing).startswith(b'%PDF')


def test_po_item_details_do_not_change_financial_totals():
    records = [dict(po_number='PO1', product_branch='Catalog', items=[dict(
        product_name='Shirt', product_branch='Item catalog', style_name='Linen', size='M', color='Blue',
        branch_name='Main', ordered_qty=3, received_qty=1, unit_cost=4)])]
    sheet = reports.build_purchase_order_item_sheet(records)
    assert sheet['totals'] == dict(ordered_qty=3, received_qty=1, line_total=12)
    assert sheet['rows'][0]['product_branch'] == 'Item catalog'
    assert sheet['rows'][0]['style_name'] == 'Linen'
    assert {'style_name', 'size', 'color', 'branch_name'} <= {c['key'] for c in sheet['columns']}