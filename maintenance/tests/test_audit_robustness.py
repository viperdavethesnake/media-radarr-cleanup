"""Audits skip symlinks and survive a single bad folder (items 5 and 6)."""
import csv
import importlib
import json
import os
import sys
from pathlib import Path

import pytest


def load(name):
    return importlib.import_module(f'maintenance.{name}')


@pytest.fixture
def library(tmp_path):
    """One real movie folder, one symlinked folder, one symlinked mkv inside the real one."""
    lib = tmp_path / 'lib'
    real = lib / 'Real_Movie_(2020)'
    real.mkdir(parents=True)
    (real / 'real.mkv').write_bytes(b'')
    (real / 'movie.nfo').write_text('<movie><title>Real Movie</title></movie>')
    os.symlink(real / 'real.mkv', real / 'link.mkv')
    os.symlink(real, lib / 'Linked_Movie_(2021)')
    return lib


def run_main(m, argv, monkeypatch):
    monkeypatch.setattr(sys, 'argv', argv)
    try:
        m.main()
    except SystemExit as e:
        assert e.code in (None, 0), e.code


# ── item 5: symlinks ──────────────────────────────────────────────────────────

def test_audit_library_skips_symlinks(library, monkeypatch, tmp_path):
    m = load('audit_library')
    out = tmp_path / 'out' / 'audit'
    out.parent.mkdir(exist_ok=True)
    run_main(m, ['audit_library.py', str(library), '--out', str(out)], monkeypatch)
    results = json.loads((out.with_suffix('.json')).read_text())
    assert [r['folder'] for r in results] == ['Real_Movie_(2020)']
    assert results[0]['mkv_count'] == 1


def test_audit_content_skips_symlinked_folder(library, monkeypatch, tmp_path):
    m = load('audit_content_vs_metadata')
    seen = []
    monkeypatch.setattr(m, 'check_folder', lambda folder, tol: seen.append(folder.name) or None)
    run_main(m, ['audit_content_vs_metadata.py', str(library)], monkeypatch)
    assert seen == ['Real_Movie_(2020)']


def test_audit_content_find_mkv_skips_symlink(library):
    m = load('audit_content_vs_metadata')
    real = library / 'Real_Movie_(2020)'
    (real / 'real.mkv').unlink()          # only the symlink remains
    assert m.find_mkv(real) is None


def test_audit_title_mismatches_skips_symlinked_folder(library, monkeypatch):
    m = load('audit_title_mismatches')
    seen = []
    monkeypatch.setattr(m, 'check_folder', lambda path: seen.append(os.path.basename(path)) or None)
    run_main(m, ['audit_title_mismatches.py', str(library)], monkeypatch)
    assert seen == ['Real_Movie_(2020)']


def test_audit_existing_folder_candidates_skip_symlinks(library):
    m = load('audit_existing_libraries')
    assert [os.path.basename(p) for p in m._folder_candidates(str(library))] == ['Real_Movie_(2020)']


# ── item 6: one bad folder must not abort the run ─────────────────────────────

def boom(*a, **k):
    raise PermissionError('simulated unreadable folder')


def test_audit_library_records_worker_error(library, monkeypatch, tmp_path):
    m = load('audit_library')
    monkeypatch.setattr(m, 'audit_folder', boom)
    out = tmp_path / 'out' / 'audit'
    out.parent.mkdir(exist_ok=True)
    run_main(m, ['audit_library.py', str(library), '--out', str(out)], monkeypatch)
    results = json.loads((out.with_suffix('.json')).read_text())
    assert results[0]['folder'] == 'Real_Movie_(2020)'
    assert any(i['check'] == 'audit_error' and i['severity'] == 'CRITICAL' for i in results[0]['issues'])


def test_audit_content_records_worker_error(library, monkeypatch, tmp_path):
    m = load('audit_content_vs_metadata')
    monkeypatch.setattr(m, 'check_folder', boom)
    csv_path = tmp_path / 'flagged.csv'
    run_main(m, ['audit_content_vs_metadata.py', str(library), '--csv', str(csv_path)], monkeypatch)
    rows = list(csv.DictReader(open(csv_path)))
    assert rows[0]['folder'] == 'Real_Movie_(2020)'
    assert 'simulated unreadable folder' in rows[0]['issue']


def test_compare_index_library_records_worker_error(library, monkeypatch):
    m = load('compare_movie_copies')
    shell = m.Shell(None)

    def flaky(folder, side, sh):
        if 'Real' in folder:
            raise TimeoutError('simulated ssh timeout')
        return None, None

    monkeypatch.setattr(m, '_index_one', flaky)
    (library / 'Other_Movie_(2019)').mkdir()
    by_key, problems = m.index_library(str(library), 'NEW', shell, threads=2)
    assert by_key == {}
    assert any('[ERROR]' in p and 'Real_Movie_(2020)' in p and 'simulated ssh timeout' in p for p in problems)
