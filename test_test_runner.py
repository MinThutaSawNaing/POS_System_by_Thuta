"""The convenience runner must not redirect test imports to the live checkout."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
import run_tests as runner


def test_absolute_in_tree_selector_is_rebased(tmp_path):
    selected = tmp_path / 'test_example.py'
    assert runner.test_arguments([str(selected) + '::Example::test_case'], tmp_path) == [
        'test_example.py::Example::test_case']


@pytest.mark.parametrize('argument', ['../test_external.py', '--pyargs', '--override-ini=pythonpath=..',
                                      '--rootdir=..', '-c', '-c../pytest.ini', '-opythonpath=..',
                                      '--basetemp=../unsafe'])
def test_external_paths_and_config_overrides_are_rejected(tmp_path, argument):
    with pytest.raises(ValueError):
        runner.test_arguments([argument], tmp_path)


def test_runner_clears_live_credentials_and_runs_in_copy(tmp_path):
    observed = {}

    def command(arguments, *, cwd, env):
        observed.update(cwd=Path(cwd), env=env, arguments=arguments)
        assert Path(cwd) != Path(runner.__file__).resolve().parent
        assert (Path(cwd) / 'app.py').is_file()
        assert not (Path(cwd) / 'instance').exists()
        assert not (Path(cwd) / 'uploads').exists()
        assert not (Path(cwd) / '.git').exists()
        return SimpleNamespace(returncode=0)

    with patch.dict(os.environ, {'APIFREE_API_KEY': 'never-pass-real-key',
                                 'AI_API_KEY': 'also-secret', 'RESEND_API_KEY': 'never-email-from-tests',
                                 'RESEND_FROM_EMAIL': 'live@example.com', 'SESSION_COOKIE_SECURE': 'true',
                                 'RESEND_SMTP_HOST': 'live-mail.example.com',
                                 'RESEND_SMTP_PORT': '587', 'RESEND_SMTP_USERNAME': 'live-user',
                                 'PYTEST_ADDOPTS': '-c ../pytest.ini'}), \
         patch.object(runner.sys, 'argv', ['run_tests.py', '-q', 'test_backup_database.py']), \
         patch.object(runner.subprocess, 'run', side_effect=command):
        assert runner.main() == 0
    assert 'APIFREE_API_KEY' not in observed['env']
    assert 'AI_API_KEY' not in observed['env']
    assert 'RESEND_API_KEY' not in observed['env']
    assert 'RESEND_FROM_EMAIL' not in observed['env']
    assert 'RESEND_SMTP_HOST' not in observed['env']
    assert 'RESEND_SMTP_PORT' not in observed['env']
    assert 'RESEND_SMTP_USERNAME' not in observed['env']
    assert 'PYTEST_ADDOPTS' not in observed['env']
    assert observed['env']['PYTHONPATH'] == str(observed['cwd'])
    assert observed['env']['SESSION_COOKIE_SECURE'] == 'false'