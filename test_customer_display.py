"""Customer display contract: public page, cashier publisher, read-only renderer."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import app

ROOT = Path(__file__).parent
needs_node = pytest.mark.skipif(shutil.which('node') is None, reason='Node required')


def test_display_page_is_public_and_calls_no_api():
    """The customer screen needs no session and must never reach the server."""
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.get('/display')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '/public/customer-display.js' in body
    # No API URL, no fetch/XHR and no authenticated data on the page itself.
    assert '/api/' not in body
    assert 'fetch(' not in body
    assert 'XMLHttpRequest' not in body
    assert 'mmqr' not in body.lower()


@needs_node
def test_customer_display_contract():
    """Run the real publisher and renderer against each other in Node."""
    result = subprocess.run(
        ['node', str(ROOT / 'public' / 'customer-display.test.js')],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])

    # Live cart: the customer sees names, quantities, prices and the total.
    assert data['cart']['kind'] == 'cart'
    assert data['cart']['count'] == 3
    assert data['cart']['total'] == 'MMK 8.15'
    assert data['cart']['items'][0] == {
        'name': 'Coffee', 'qty': 2, 'unit': 'cup',
        'unitPrice': 'MMK 2.50', 'lineTotal': 'MMK 5.00',
    }
    assert data['cartSaleHidden'] is False
    assert data['cartTotalShown'] == 'MMK 8.15'
    assert data['cartItemsShown'] == 2

    # The QR freezes the sale; the amount is the order amount, not the cart total.
    assert data['qr']['kind'] == 'qr'
    assert data['qr']['payment']['amount'] == 'MMK 8.15'
    assert data['qr']['payment']['orderId'] == 'ORD-9'
    assert data['qr']['payment']['deadlineAt'] > 0
    assert data['qrPanelHidden'] is False
    assert data['qrImage'] == 'data:image/png;base64,AAA'
    assert data['timerText'] == '1:00'

    # Editing the cart after the QR exists must not change what is displayed.
    assert data['afterEdit']['kind'] == 'qr'
    assert data['afterEdit']['payment']['amount'] == 'MMK 8.15'
    assert data['afterEdit']['total'] == 'MMK 8.15'

    # Expiry comes from the deadline, and the customer is told to ask the cashier.
    assert data['expiredBefore'] is True
    assert data['expiredAfter'] is False
    assert data['expiredImageHidden'] is True

    # Cashier tab closed: content is held and the warning is shown.
    assert data['bannerHidden'] is False
    assert data['heldQr'] is False

    # Logout or branch switch clears the screen instead of holding stale data.
    assert data['resetMessage'] == 'reset'
    assert data['resetIdleHidden'] is False
    assert data['resetSaleHidden'] is True

    # A cash sale ends on the thank-you screen with the change due.
    assert data['paid']['kind'] == 'paid'
    assert data['paid']['total'] == 'MMK 2.00'
    assert data['paid']['change'] == 'MMK 3.00'
    assert data['paidPanelHidden'] is False
    assert data['paidTotalShown'] == 'MMK 2.00'
    assert data['paidChangeShown'] == 'MMK 3.00'


@needs_node
def test_publisher_ignores_a_second_cashier_tab():
    """A second cashier tab must not make the customer screen flicker."""
    source = (ROOT / 'public' / 'customer-display.js').read_text(encoding='utf-8')
    assert 'ADOPT_QUIET_MS' in source
    assert 'message.publisherId !== publisherId' in source
