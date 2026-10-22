"""Exercise the spreadsheet preview/confirmation boundary without production data."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_product_import_preview_and_confirmation():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for JavaScript runtime tests')
    root = Path(__file__).resolve().parent
    result = subprocess.run(
        [node, '--test', str(root / 'public' / 'product-import.test.js')],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + '\n' + result.stderr