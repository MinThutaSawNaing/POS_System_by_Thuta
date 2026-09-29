"""Regression tests for deliberate, explicit configuration saves."""

from pathlib import Path


DASHBOARD = Path('templates/dashboard.html').read_text(encoding='utf-8')


def test_each_independent_configuration_card_has_a_save_button():
    expected = {
        'save-general-settings-btn': 'saveSettings()',
        'save-receipt-customization-btn': 'saveReceiptCustomization()',
        'save-mmqr-btn': 'saveMMQRConfiguration()',
        'save-receipt-printer-btn': 'saveReceiptPrinterSettings()',
        'save-label-printing-btn': 'saveLabelPrintingSettings()',
        'save-agent-autonomy-btn': 'saveAgentAutonomy()',
    }
    for button_id, handler in expected.items():
        assert f'id="{button_id}"' in DASHBOARD
        assert f'onclick="{handler}"' in DASHBOARD


def test_language_selection_does_not_save_immediately():
    language_select = DASHBOARD.split('id="settings-language"', 1)[1].split('</select>', 1)[0]
    assert 'onchange=' not in language_select
    assert 'changeLanguage(language, { silent: true })' in DASHBOARD


def test_mmqr_selection_and_removal_are_staged_until_save():
    assert 'stageMMQRFile(event.target.files?.[0])' in DASHBOARD
    assert 'pendingMMQRFile = file' in DASHBOARD
    assert 'pendingMMQRRemoval = true' in DASHBOARD
    assert 'function saveMMQRConfiguration()' in DASHBOARD


def test_autonomy_toggle_does_not_post_on_change():
    load_block = DASHBOARD.split('function loadAgentAutonomy()', 1)[1].split(
        'function saveAgentAutonomy()', 1
    )[0]
    assert 'method: "POST"' not in load_block
    assert 'addEventListener("change"' not in load_block
    save_block = DASHBOARD.split('function saveAgentAutonomy()', 1)[1].split(
        'function toggleApiKeyVisibility()', 1
    )[0]
    assert 'method: "POST"' in save_block
    assert '/api/agent/autonomy' in save_block


def test_scoped_save_functions_send_only_their_own_configuration():
    receipt_block = DASHBOARD.split('function saveReceiptPrinterSettings()', 1)[1].split(
        'function saveLabelPrintingSettings()', 1
    )[0]
    assert 'receipt_paper_size' in receipt_block
    assert 'label_geometry' not in receipt_block

    label_block = DASHBOARD.split('function saveLabelPrintingSettings()', 1)[1].split(
        'function saveSettings()', 1
    )[0]
    assert 'label_geometry' in label_block
    assert 'receipt_paper_size' not in label_block