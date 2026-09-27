"""Maintenance scripts honour RC_MEDIA_BASE and load .env."""
import os
import runpy
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MAINT = REPO / 'maintenance'


def load(name, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return runpy.run_path(str(MAINT / f'{name}.py'), run_name='not_main')


@pytest.mark.parametrize('script,attr,expect', [
    ('codec_bitrate_report', 'BASE', Path('/x')),
    ('orphaned_metadata_cleanup', 'BASE', Path('/x')),
    ('orphaned_tv_metadata_report', 'TV_ROOT', Path('/x/tvshows')),
    ('audit_existing_libraries', 'DEFAULT_MOVIES_DIR', '/x/movies'),
    ('audit_existing_libraries', 'DEFAULT_DOCS_DIR', '/x/documentaries'),
    ('audit_existing_libraries', 'DEFAULT_MOVE_DEST', '/x/working/movies'),
    ('fix_media_perms', 'BASE', '/x'),
])
def test_default_paths_derive_from_media_base(script, attr, expect, monkeypatch):
    m = load(script, monkeypatch, {'RC_MEDIA_BASE': '/x'})
    assert m[attr] == expect


@pytest.mark.parametrize('script', [
    'codec_bitrate_report', 'orphaned_metadata_cleanup', 'orphaned_tv_metadata_report',
    'audit_existing_libraries', 'audit_title_mismatches', 'fix_media_perms',
])
def test_script_loads_dotenv(script, monkeypatch):
    calls = []
    fake = types.ModuleType('dotenv')
    fake.load_dotenv = lambda *a, **k: calls.append(a) or True
    monkeypatch.setitem(sys.modules, 'dotenv', fake)
    load(script, monkeypatch, {})
    assert calls, f'{script} never called load_dotenv()'


def test_fix_media_perms_sudo_reexec_carries_env(monkeypatch):
    m = load('fix_media_perms', monkeypatch,
             {'RC_MEDIA_BASE': '/x', 'RC_MEDIA_OWNER': 'bob', 'RC_MEDIA_GROUP': 'staff'})
    captured = {}

    def fake_execvp(prog, argv):
        captured['prog'], captured['argv'] = prog, argv
        raise SystemExit(0)

    monkeypatch.setattr(os, 'execvp', fake_execvp)
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    m['which'] = lambda _: '/usr/bin/sudo'
    monkeypatch.setattr(sys, 'argv', ['fix_media_perms.py', '--apply', '/x/movies'])
    with pytest.raises(SystemExit):
        m['main']()
    argv = captured['argv']
    assert argv[0] == 'sudo'
    assert 'RC_MEDIA_BASE=/x' in argv and 'RC_MEDIA_OWNER=bob' in argv and 'RC_MEDIA_GROUP=staff' in argv
    assert argv[-2:] == ['--apply', '/x/movies']


# ── library defaults derive from RC_MEDIA_BASE (not literal /storage/media) ──

@pytest.mark.parametrize('script,attr,expect', [
    ('audit_library', 'DEFAULT_SCAN_DIR', '/x/movies'),
    ('audit_content_vs_metadata', 'DEFAULT_SCAN_DIR', '/x/movies'),
    ('audit_decode_integrity', 'DEFAULT_SCAN_DIR', '/x/movies'),
    ('regenerate_nfos', 'DEFAULT_SCAN_DIRS', ['/x/movies', '/x/documentaries']),
    ('compare_movie_copies', 'DEFAULT_EXISTING_PATH', '/x/movies,/x/movies2'),
    ('local_vs_remote_compare', 'DEFAULT_LOCAL_PATH', '/x/movies'),
])
def test_library_defaults_derive_from_media_base(script, attr, expect, monkeypatch):
    monkeypatch.setenv('RC_VERIFY_SCAN_DIR', '')   # .env would re-set it if merely deleted
    monkeypatch.setenv('TMDB_API_KEY', 'x')
    m = load(script, monkeypatch, {'RC_MEDIA_BASE': '/x'})
    assert m[attr] == expect


def test_audit_title_mismatches_default_derives_from_media_base(monkeypatch, capsys):
    monkeypatch.setenv('RC_VERIFY_SCAN_DIR', '')   # .env would re-set it if merely deleted
    m = load('audit_title_mismatches', monkeypatch, {'RC_MEDIA_BASE': '/x'})
    monkeypatch.setattr(sys, 'argv', ['audit_title_mismatches.py'])
    with pytest.raises(SystemExit):
        m['main']()
    assert 'Not a directory: /x/movies' in capsys.readouterr().out


def test_verify_scan_dir_override_still_wins(monkeypatch):
    m = load('audit_library', monkeypatch, {'RC_MEDIA_BASE': '/x', 'RC_VERIFY_SCAN_DIR': '/elsewhere'})
    assert m['DEFAULT_SCAN_DIR'] == '/elsewhere'
