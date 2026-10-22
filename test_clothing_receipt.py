"""Optional return metadata stays visible without changing receipt accounting."""

from flask import Flask, render_template

from receipt import build_return_exchange_view


def test_damaged_reason_is_visible_and_html_escaped():
    record = dict(reason='Fit <test>', return_total=105, net_total=-105,
        refund_amount=105, return_items=[dict(name='Shirt — Black / M', quantity=1,
        unit_price=100, line_total=100, line_tax=5, tax_rate=5,
        reason='<script>unsafe</script>', disposition='damaged')])
    view = build_return_exchange_view(record, '58mm')
    assert view['reason'] == record['reason']
    assert view['return_items'][0]['disposition'] == 'damaged'
    assert view['return_items'][0]['reason'] == '<script>unsafe</script>'
    assert view['refund_amount'] == 105
    app = Flask(__name__)
    with app.app_context():
        html = render_template('exchange_receipt.html', exchange=view)
    assert 'damaged (not restocked)' in html
    assert '&lt;script&gt;unsafe&lt;/script&gt;' in html
    assert '<script>unsafe</script>' not in html
    assert 'Black / M' in html


def test_legacy_return_metadata_defaults_to_sellable():
    view = build_return_exchange_view(dict(return_items=[dict(name='Old item', quantity=1)]), '80mm')
    assert view['reason'] == ''
    assert view['return_items'][0]['disposition'] == 'sellable'