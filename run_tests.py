"""Run pytest in a disposable checkout, never against local POS data.

Usage: python run_tests.py [pytest arguments...]
All application imports (including live-server subprocesses) see only the copied
source and a fresh instance/uploads directory. No deployment secrets are copied.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def test_arguments(arguments, source):
    """Accept ordinary pytest selection, never paths escaping the copied tree."""
    safe = []
    blocked = ('--pyargs', '--override-ini', '--rootdir', '--confcutdir',
               '--basetemp', '--import-mode', '--junitxml', '--junit-xml',
               '--log-file', '--debug', '--config-file', '--doctest')
    for argument in arguments:
        if argument.startswith(('-c', '-o')) or argument.startswith(blocked):
            raise ValueError(f'Unsafe pytest option for isolated runner: {argument}')
        # File selectors may carry a ::Class::test suffix. Normalize in-tree
        # absolute paths; reject every traversal/external path before pytest.
        selector, *suffix = argument.split('::')
        candidate = Path(selector)
        if candidate.is_absolute() or '..' in candidate.parts:
            resolved = candidate.resolve() if candidate.is_absolute() else (source / candidate).resolve()
            try:
                relative = resolved.relative_to(source)
            except ValueError:
                raise ValueError(f'Test path must be inside the source checkout: {selector}') from None
            argument = '::'.join([str(relative), *suffix])
        safe.append(argument)
    return safe


def main():
    source = Path(__file__).resolve().parent
    try:
        arguments = test_arguments(sys.argv[1:] or ['-q', '--tb=short'], source)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    excluded = shutil.ignore_patterns(
        '.git', '.env', '.env.*', 'instance', 'uploads', '__pycache__',
        '.pytest_cache', '.scratch', '.venv', 'venv', 'node_modules', '*.pyc',
        '*.db', '*.db-*', '*.sqlite', '*.sqlite3', '*.bak',
    )
    with tempfile.TemporaryDirectory(prefix='pos-tests-') as directory:
        checkout = Path(directory) / 'source'
        shutil.copytree(source, checkout, ignore=excluded)
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(('AI_', 'POS_ACCOUNT_BARRIER_', 'POS_PERMISSION_')):
                env.pop(key)
        for key in ('APIFREE_API_KEY', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
            env.pop(key, None)
        env.update(
            SECRET_KEY='disposable-pos-tests-only',
            PYTHONPATH=str(checkout),
            PYTHONDONTWRITEBYTECODE='1',
            PYTHONIOENCODING='utf-8',
        )
        print('Running tests in disposable checkout:', checkout, flush=True)
        result = subprocess.run(
            [sys.executable, '-B', '-m', 'pytest', '-p', 'no:cacheprovider', *arguments],
            cwd=checkout, env=env,
        )
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())