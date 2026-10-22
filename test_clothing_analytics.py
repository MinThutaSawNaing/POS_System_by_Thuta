"""Clothing report/promotion HTTP boundaries; run through run_tests.py only."""

import json
import uuid

import pytest


@pytest.fixture
def catalog():
    import app as pos
    with pos.app.app_context():
        pos.app.config['TESTING'] = True
        tag = uuid.uuid4().hex
        branch = pos.Branch(name='Clothing ' + tag, code=tag[:12], is_active=True)
        foreign = pos.Branch(name='Foreign ' + tag, code='F' + tag[:11], is_active=True)
        style = pos.ClothingStyle(name='Cotton Shirt', code=tag, code_norm=tag)
        pos.db.session.add_all([branch, foreign, style])
        pos.db.session.flush()
        products = []
        for size, stock in [('M', 5), ('L', 0)]:
            identity = uuid.uuid4().hex
            pos.db.session.add(pos.ClothingVariant(catalog_id=identity, style_id=style.id,
                size=size, color='Black', size_norm=size.lower(), color_norm='black'))
            product = pos.Product(name='Shirt', catalog_id=identity, barcode=identity,
                stock=stock, price=100, tax_rate=5, branch_id=branch.id)
            pos.db.session.add(product)
            products.append(product)
        other = pos.Product(name='Other shirt', barcode=uuid.uuid4().hex, stock=17,
            price=100, branch_id=foreign.id, catalog_id=products[0].catalog_id)
        pos.db.session.add(other)
        user = pos.User.query.filter_by(username='admin').first()
        sale = pos.Sale(transaction_id=tag, total=210, tax=10, user_id=user.id, branch_id=branch.id)
        pos.db.session.add(sale)
        pos.db.session.flush()
        snapshot = dict(name='Cotton Shirt — Black / M', style_id=style.id, style_name=style.name,
            variant_id=pos.ClothingVariant.query.filter_by(catalog_id=products[0].catalog_id).one().id,
            size='M', color='Black')
        pos.db.session.add_all([
            pos.SaleItem(sale_id=sale.id, product_id=products[0].id, quantity=1, price=100, tax=5,
                display_snapshot=json.dumps(snapshot)),
            pos.SaleItem(sale_id=sale.id, product_id=products[0].id, quantity=1, price=100, tax=5),
        ])
        pos.db.session.commit()
        client = pos.app.test_client()
        with client.session_transaction() as state:
            state.update(user_id=user.id, role='manager', branch_id=branch.id)
        yield pos, client, style, products, branch, foreign, sale
        pos.db.session.rollback()
        pos.Promotion.query.filter(pos.Promotion.product_id.in_([p.id for p in products])).delete(synchronize_session=False)
        pos.SaleItem.query.filter_by(sale_id=sale.id).delete()
        pos.db.session.delete(sale)
        pos.Product.query.filter(pos.Product.branch_id.in_([branch.id, foreign.id])).delete(synchronize_session=False)
        pos.ClothingVariant.query.filter_by(style_id=style.id).delete()
        pos.db.session.delete(style)
        pos.db.session.delete(branch)
        pos.db.session.delete(foreign)
        pos.db.session.commit()


def test_report_preserves_financial_totals_and_does_not_reclassify_legacy(catalog):
    pos, client, style, products, branch, foreign, sale = catalog
    response = client.get('/api/reports/clothing')
    assert response.status_code == 200
    report = response.get_json()
    assert {row['branch_id'] for row in report['stock']} == {branch.id}
    assert sum(row['stock'] for row in report['stock']) == 5
    assert report['financial_totals'] == {'sale_total': 210, 'sale_tax': 10}
    classified = next(row for row in report['sales'] if row['style_id'] == style.id)
    legacy = next(row for row in report['sales'] if row['style_id'] is None)
    assert classified['net_total'] == legacy['net_total'] == 105
    assert classified['size'] == 'M' and classified['color'] == 'Black'
    assert report['stock_age_available'] is False
    widened = client.get('/api/reports/clothing?scope=all').get_json()
    assert {branch.id, foreign.id}.issubset({row['branch_id'] for row in widened['stock']})


def test_style_promotions_expand_without_duplicate_or_stacked_discounts(catalog):
    pos, client, style, products, *_ = catalog
    payload = dict(discount_type='percent', discount_value=10,
        start_date='2020-01-01T00:00:00', end_date='2090-01-01T00:00:00')
    endpoint = f'/api/clothing/styles/{style.id}/promotions'
    first = client.post(endpoint, json=payload)
    assert first.status_code == 201, first.get_json()
    assert client.post(endpoint, json=payload).get_json()['promotion_ids'] == first.get_json()['promotion_ids']
    assert pos.Promotion.query.filter(pos.Promotion.product_id.in_([p.id for p in products])).count() == 2
    selected = dict(payload, product_ids=[products[0].id], discount_type='fixed', discount_value=5)
    assert client.post(endpoint, json=selected).status_code == 201
    pos.db.session.expire_all()
    # Existing rules expose independent allowed prices, never compounded 85.50.
    assert set(map(float, pos.active_promotion_prices(products[0]))) == {90, 95}
    assert client.post(endpoint, json=dict(payload, product_ids=[-1])).status_code == 400
    assert client.post(endpoint, json=dict(payload, discount_value='NaN')).status_code == 400


def test_clothing_analytics_deny_cashier_and_anonymous(catalog):
    pos, client, style, *_ = catalog
    with client.session_transaction() as state:
        state['role'] = 'cashier'
    assert client.get('/api/reports/clothing?scope=all').status_code == 403
    assert client.post(f'/api/clothing/styles/{style.id}/promotions', json={}).status_code == 403
    assert pos.app.test_client().get('/api/reports/clothing').status_code in (401, 403)