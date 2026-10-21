"""Exercise widget currency text against the real dashboard observer normalizer."""

import json
import subprocess
from pathlib import Path

from test_dashboard_money_javascript import NODE, _javascript_function, pytestmark


def test_grouped_widget_currency_survives_dashboard_normalization():
    templates = Path(__file__).parent / 'templates'
    widget = (templates / 'ai_agent_widget.html').read_text(encoding='utf-8')
    dashboard = (templates / 'dashboard.html').read_text(encoding='utf-8')
    functions = '\n'.join([
        _javascript_function(widget, 'normalizeAssistantCurrency'),
        _javascript_function(widget, 'renderAssistantMarkdown'),
        _javascript_function(dashboard, 'formatCurrencyWithSymbol'),
        _javascript_function(dashboard, 'normalizeCurrencyInText'),
    ])
    script = functions + r'''
const APP_SETTINGS = {currencySuffix: 'MMK'};
const inputs = ['$7,870,400.00', '$1,234.56 / $12.50', '$-1,234.50', '$0.00'];
const outputs = [];
for (const mode of ['fallback', 'markdown', 'error']) {
  global.window = mode === 'fallback' ? {} : {
    marked: {parse: text => {if (mode === 'error') throw Error('parse'); return text;}},
    DOMPurify: {sanitize: text => text}
  };
  console.warn = () => {};
  for (const text of inputs) {
    const bubble = {querySelectorAll: () => []};
    renderAssistantMarkdown(bubble, text);
    outputs.push(normalizeCurrencyInText(bubble.innerHTML || bubble.textContent));
  }
}
console.log(JSON.stringify(outputs));
'''
    result = subprocess.run([NODE, '-e', script], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == [
        '7870400.00 MMK', '1234.56 MMK / 12.50 MMK', '-1234.50 MMK', '0.00 MMK'
    ] * 3