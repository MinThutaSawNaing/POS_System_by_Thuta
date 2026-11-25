"""Regression tests for deliberate, explicit configuration saves."""

from pathlib import Path


DASHBOARD = Path('templates/dashboard.html').read_text(encoding='utf-8')


def test_each_independent_configuration_card_has_a_save_button():
    expected = {
        'save-general-settings-btn': 'saveSettings()',
        'save-automated-offline-fetch-btn': 'saveAutomatedOfflineFetch()',
        'save-receipt-customization-btn': 'saveReceiptCustomization()',
        'save-mmqr-btn': 'saveMMQRConfiguration()',
        'save-receipt-printer-btn': 'saveReceiptPrinterSettings()',
        'save-label-printing-btn': 'saveLabelPrintingSettings()',
        'save-mmpay-btn': 'savePaymentSettings()',
    }
    for button_id, handler in expected.items():
        assert f'id="{button_id}"' in DASHBOARD
        assert f'onclick="{handler}"' in DASHBOARD


def test_offline_mode_has_a_discoverable_settings_group_and_pos_indicator():
    assert 'data-group="offline"' in DASHBOARD
    assert 'id="settings-group-offline"' in DASHBOARD
    assert 'id="settings-automated-offline-fetch"' in DASHBOARD
    assert 'id="pos-offline-mode"' in DASHBOARD
    general_save = DASHBOARD.split('function saveSettings()', 1)[1].split('function saveAutomatedOfflineFetch()', 1)[0]
    assert 'automated_offline_fetch' not in general_save
    offline_save = DASHBOARD.split('function saveAutomatedOfflineFetch()', 1)[1].split('// ===== MOQ', 1)[0]
    assert 'automated_offline_fetch' in offline_save


def test_language_selection_does_not_save_immediately():
    language_select = DASHBOARD.split('id="settings-language"', 1)[1].split('</select>', 1)[0]
    assert 'onchange=' not in language_select
    assert 'changeLanguage(language, { silent: true })' in DASHBOARD


def test_mmqr_selection_and_removal_are_staged_until_save():
    assert 'stageMMQRFile(event.target.files?.[0])' in DASHBOARD
    assert 'pendingMMQRFile = file' in DASHBOARD
    assert 'pendingMMQRRemoval = true' in DASHBOARD
    assert 'function saveMMQRConfiguration()' in DASHBOARD


def test_autonomy_toggle_is_gone():
    """Autonomy is no longer a setting: the assistant always acts."""
    assert 'agent-autonomy-toggle' not in DASHBOARD
    assert 'saveAgentAutonomy' not in DASHBOARD
    assert '/api/agent/autonomy' not in DASHBOARD


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


def test_sidebar_translation_uses_section_names_not_fragile_positions():
    translation_block = DASHBOARD.split('// Update sidebar navigation', 1)[1].split(
        '// Update section titles', 1
    )[0]
    assert 'navKeys = [' not in translation_block
    assert "showSection\\('([^']+)'\\)" in translation_block
    assert 'translations[currentLanguage]?.[translationKey]' in translation_block
    assert 'logs: "Logs"' in DASHBOARD
    assert 'logs: "စနစ်မှတ်တမ်းများ"' in DASHBOARD
    assert '"logs-section h2": "logs"' in DASHBOARD


def test_logs_are_lazy_paged_and_scrolled_inside_their_card():
    assert 'logs: 30,' in DASHBOARD
    assert 'logs: () => loadLogs()' in DASHBOARD
    assert 'SECTION_ALWAYS_RENDER' not in DASHBOARD
    assert 'if (sectionNeedsRender(sectionId))' in DASHBOARD
    assert 'savedSection === "logs"' in DASHBOARD
    assert 'loadLogs();' not in DASHBOARD.split('// Load only what the boot screen needs', 1)[1].split(
        'showSection(startSection);', 1
    )[0]
    assert 'id="logs-table-scroll"' in DASHBOARD
    assert 'class="table-responsive audit-table-scroll"' in DASHBOARD
    assert 'max-height: min(62vh, 620px)' in DASHBOARD
    assert 'id="logs-pagination"' in DASHBOARD
    assert 'logsRequestGeneration' in DASHBOARD
    assert 'sectionStateOf("logs").rendered = false' in DASHBOARD