"""Backups keep known settings without leaking credential assignments or continuations."""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backup_settings", Path(__file__).resolve().parents[2] / "scripts" / "backup_settings.py")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
settings = _MODULE.settings


def test_backup_settings_allowlist():
    assert settings('export HO_VERSION = dev\nHO_ORCHESTRATION=true\n'
                    'export TELEGRAM_BOT_TOKEN=fake-token\nOTHER_CREDENTIAL=fake-secret\n') == (
        'HO_VERSION=dev\nHO_ORCHESTRATION=true\n')


def test_backup_settings_discards_multiline_credentials_and_settings():
    assert settings('TELEGRAM_BOT_TOKEN="first\nHO_VERSION=secret-continuation\nlast"\n'
                    "HO_LOG_LEVEL='first\nsecond'\nHO_VERSION=dev\n") == 'HO_VERSION=dev\n'


def test_backup_settings_unclosed_secret_fails_closed():
    assert settings('HO_VERSION=dev\nAPI_KEY="first\nHO_LOG_LEVEL=secret\n') == 'HO_VERSION=dev\n'


def test_backup_settings_preserves_single_line_quoted_values():
    assert settings('HO_PROJECTS_ROOT_HOST="/srv/My Projects"\n# comment\n') == (
        'HO_PROJECTS_ROOT_HOST="/srv/My Projects"\n')
