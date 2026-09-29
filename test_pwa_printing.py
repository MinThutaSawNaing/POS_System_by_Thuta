"""Regression coverage for printing without leaving installed PWA mode."""

from pathlib import Path


DASHBOARD = Path('templates/dashboard.html').read_text(encoding='utf-8')


def test_installed_pwa_detection_supports_chromium_and_ios():
    assert 'function isStandalonePWA()' in DASHBOARD
    assert 'display-mode: standalone' in DASHBOARD
    assert 'navigator.standalone === true' in DASHBOARD


def test_same_origin_print_frame_is_created_and_cleaned_up():
    assert 'function createPwaPrintFrame()' in DASHBOARD
    assert 'className = "pwa-print-frame"' in DASHBOARD
    assert 'frame.contentWindow?.addEventListener("afterprint"' in DASHBOARD
    assert 'frame.addEventListener("load", registerAfterPrintCleanup)' in DASHBOARD
    assert 'setTimeout(cleanup, 120000)' in DASHBOARD


def test_receipt_and_delivery_prints_use_in_app_frame_in_pwa_mode():
    receipt = DASHBOARD.split('function openReceiptWindow(', 1)[1].split(
        '// ============================================', 1
    )[0]
    assert 'const standalone = isStandalonePWA();' in receipt
    assert 'if (standalone)' in receipt
    assert 'loadUrlInPwaPrintFrame' in receipt

    delivery = DASHBOARD.split('function printDeliverySlip(', 1)[1].split(
        '// ==================== Delivery Performance Report', 1
    )[0]
    assert 'if (isStandalonePWA())' in delivery
    assert 'loadUrlInPwaPrintFrame' in delivery


def test_barcode_post_targets_frame_in_pwa_mode_not_blank_window():
    block = DASHBOARD.split('function generateBarcodeLabels()', 1)[1].split(
        '// ============================================', 1
    )[0]
    assert 'const pwaPrintFrame = isStandalonePWA() ? createPwaPrintFrame() : null;' in block
    assert 'form.target = pwaPrintFrame?.name || "_blank";' in block


def test_offline_receipt_uses_print_frame_in_pwa_mode():
    block = DASHBOARD.split('function openOfflineReceiptWindow(', 1)[1].split(
        'function openReceiptWindow(', 1
    )[0]
    assert 'typeof isStandalonePWA === "function" && isStandalonePWA()' in block
    assert 'printFrame?.contentWindow' in block
    assert 'const shouldAutoPrint = autoPrint || Boolean(printFrame);' in block


def test_pwa_receipt_forces_autoprint_even_for_history_preview_call():
    block = DASHBOARD.split('function openReceiptWindow(', 1)[1].split(
        '// ============================================', 1
    )[0]
    assert 'const suffix = autoPrint || standalone ? "?autoprint=1" : "";' in block