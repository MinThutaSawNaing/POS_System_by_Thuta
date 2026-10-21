"""Run the local payment state-machine regressions with the normal test runner."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_myanmyanpay_checkout_state_machine():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for JavaScript runtime tests')
    root = Path(__file__).resolve().parent
    result = subprocess.run([node, '--test', str(root / 'public' / 'myanmyanpay.test.js')],
                            cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + '\n' + result.stderr