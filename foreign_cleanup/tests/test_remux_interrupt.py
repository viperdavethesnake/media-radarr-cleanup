"""Ctrl-C on remux_foreign kills in-flight mkvmerge, removes partial output, cancels the rest."""
import importlib
import os
import subprocess
import threading
import time

import pytest


@pytest.fixture
def rf(tmp_path, monkeypatch):
    monkeypatch.setenv('RC_SERVARR_DIR', str(tmp_path))
    monkeypatch.setenv('TMDB_API_KEY', 'x')
    from foreign_cleanup import remux_foreign as m
    importlib.reload(m)
    m.shutdown_requested = False
    m._live_procs.clear()
    return m


# ── Task 1: registry + handler + run_mkvmerge ────────────────────────────────

def test_signal_handler_terminates_registered_process(rf, monkeypatch):
    monkeypatch.setattr(rf, 'REMUX_TIMEOUT', 30)
    result = {}

    def worker():
        try:
            rf.run_mkvmerge(['sleep', '30'], 'film')
        except subprocess.CalledProcessError as e:
            result['rc'] = e.returncode

    t = threading.Thread(target=worker)
    t.start()
    for _ in range(50):                       # wait for registration
        if rf._live_procs:
            break
        time.sleep(0.02)
    assert rf._live_procs, 'process was never registered'
    rf.signal_handler(2, None)
    t.join(timeout=5)
    assert not t.is_alive()
    assert rf.shutdown_requested is True
    assert result['rc'] != 0
    assert not rf._live_procs                 # deregistered in finally


def test_second_signal_hard_exits(rf, monkeypatch):
    calls = []
    monkeypatch.setattr(os, '_exit', lambda code: calls.append(code))
    rf.signal_handler(2, None)
    assert calls == []
    rf.signal_handler(2, None)
    assert calls == [130]


def test_timeout_kills_process(rf, monkeypatch):
    monkeypatch.setattr(rf, 'REMUX_TIMEOUT', 0.2)
    with pytest.raises(subprocess.TimeoutExpired):
        rf.run_mkvmerge(['sleep', '3107'], 'film')
    assert not rf._live_procs
    out = subprocess.run(['pgrep', '-f', '^sleep 3107$'], capture_output=True, text=True)
    assert out.stdout.strip() == '', 'sleep 3107 still running after timeout'


def test_warning_exit_keeps_output_and_returns(rf):
    rf.run_mkvmerge(['sh', '-c', 'exit 1'], 'film')      # no raise


def test_fatal_exit_raises(rf):
    with pytest.raises(subprocess.CalledProcessError):
        rf.run_mkvmerge(['sh', '-c', 'exit 2'], 'film')


# ── Task 2: cleanup + interrupted result ─────────────────────────────────────

INFO = {'tracks': [
    {'id': 0, 'type': 'video', 'properties': {}},
    {'id': 1, 'type': 'audio', 'properties': {'language': 'chi', 'codec_id': 'A_TRUEHD'}},
]}


def _spec(src_folder):
    return {'folder': 'Film_(2001)', 'src_folder': str(src_folder), 'video': 0,
            'audio_default': 1, 'output_name': 'Film_(2001)_[1080p_hevc_truehd].mkv'}


@pytest.fixture
def film(rf, tmp_path, monkeypatch):
    """A source folder with one 'mkv', identify/TMDB stubbed, build_cmd replaced per test."""
    src = tmp_path / 'foreign' / 'Film_(2001)'
    src.mkdir(parents=True)
    (src / 'source.mkv').write_bytes(b'')
    monkeypatch.setattr(rf, 'mkv_identify', lambda path: INFO)
    monkeypatch.setattr(rf, 'validate', lambda spec, info: [])
    dst_folder = tmp_path / 'cleaned' / 'Film_(2001)'
    return src, dst_folder


def _cmd_writing_partial_then(dst_folder, exit_code, sleep=0):
    """build_cmd stand-in: touch the output file, then either exit with code or block in
    sleep. `exec` replaces sh with sleep so terminate()/kill() reach the sleeper directly."""
    def build(spec, src, dst):
        if sleep:
            return ['sh', '-c', f'touch "{dst}"; exec sleep {sleep}']
        return ['sh', '-c', f'touch "{dst}"; exit {exit_code}']
    return build


def test_mkvmerge_failure_removes_partial_file(rf, film, monkeypatch):
    src, dst_folder = film
    monkeypatch.setattr(rf, 'build_cmd', _cmd_writing_partial_then(dst_folder, 2))
    r = rf.process(_spec(src), True)
    assert r['ok'] is False and 'mkvmerge failed' in r['error']
    assert not r.get('interrupted')
    assert not dst_folder.exists()


def test_cleanup_keeps_previous_output_in_same_folder(rf, film, monkeypatch):
    src, dst_folder = film
    dst_folder.mkdir(parents=True)
    keep = dst_folder / 'movie.nfo'
    keep.write_text('<movie/>')
    monkeypatch.setattr(rf, 'build_cmd', _cmd_writing_partial_then(dst_folder, 2))
    rf.process(_spec(src), True)
    assert keep.exists()
    assert not (dst_folder / 'Film_(2001)_[1080p_hevc_truehd].mkv').exists()


def test_interrupt_mid_remux_reports_interrupted_and_cleans(rf, film, monkeypatch):
    src, dst_folder = film
    monkeypatch.setattr(rf, 'REMUX_TIMEOUT', 30)
    monkeypatch.setattr(rf, 'build_cmd', _cmd_writing_partial_then(dst_folder, 0, sleep=30))
    result = {}
    t = threading.Thread(target=lambda: result.update(rf.process(_spec(src), True)))
    t.start()
    for _ in range(100):
        if rf._live_procs:
            break
        time.sleep(0.02)
    rf.signal_handler(2, None)
    t.join(timeout=5)
    assert result == {'folder': 'Film_(2001)', 'ok': False, 'error': 'interrupted', 'interrupted': True}
    assert not dst_folder.exists()


def test_flag_set_after_success_is_still_ok(rf, film, monkeypatch):
    src, dst_folder = film
    monkeypatch.setattr(rf, 'build_cmd', _cmd_writing_partial_then(dst_folder, 0))
    rf.shutdown_requested = True          # e.g. Ctrl-C during regen_metadata
    r = rf.process(_spec(src), True)
    assert r['ok'] is True
    assert (dst_folder / 'Film_(2001)_[1080p_hevc_truehd].mkv').exists()


def test_timeout_removes_partial_file(rf, film, monkeypatch):
    src, dst_folder = film
    monkeypatch.setattr(rf, 'REMUX_TIMEOUT', 0.2)
    monkeypatch.setattr(rf, 'build_cmd', _cmd_writing_partial_then(dst_folder, 0, sleep=30))
    r = rf.process(_spec(src), True)
    assert r['ok'] is False and 'exceeded' in r['error']
    assert not dst_folder.exists()


# ── Fix round 1: never discard a file this run did not start writing ─────────

def test_exception_before_mkvmerge_keeps_preexisting_output(rf, film, monkeypatch):
    src, dst_folder = film
    dst_folder.mkdir(parents=True)
    existing = dst_folder / 'Film_(2001)_[1080p_hevc_truehd].mkv'
    existing.write_bytes(b'old')

    def boom(spec, src, dst):
        raise RuntimeError('boom')
    monkeypatch.setattr(rf, 'build_cmd', boom)
    r = rf.process(_spec(src), True)
    assert r['ok'] is False and 'boom' in r['error']
    assert existing.exists()
    assert existing.read_bytes() == b'old'


def test_dry_run_exception_keeps_preexisting_output(rf, film, monkeypatch):
    src, dst_folder = film
    dst_folder.mkdir(parents=True)
    existing = dst_folder / 'Film_(2001)_[1080p_hevc_truehd].mkv'
    existing.write_bytes(b'old')

    def boom(spec, src, dst):
        raise RuntimeError('boom')
    monkeypatch.setattr(rf, 'build_cmd', boom)
    rf.process(_spec(src), False)
    assert existing.exists()
    assert existing.read_bytes() == b'old'


# ── Task 3: main loop ────────────────────────────────────────────────────────

def _specs(n):
    return [{'folder': f'F{i}', 'src_folder': '/nowhere', 'video': 0, 'audio_default': 1} for i in range(n)]


def test_pending_films_cancelled_after_interrupt(rf, monkeypatch, tmp_path):
    started = []
    gate = threading.Event()

    def fake_process(spec, apply_changes):
        started.append(spec['folder'])
        if spec['folder'] == 'F0':
            rf.shutdown_requested = True
            return {'folder': 'F0', 'ok': False, 'error': 'interrupted', 'interrupted': True}
        gate.wait(2)
        return {'folder': spec['folder'], 'ok': True, 'size_gb': 1, 'seconds': 1}

    monkeypatch.setattr(rf, 'process', fake_process)
    specfile = tmp_path / 'spec.json'
    specfile.write_text(__import__('json').dumps(_specs(6)))
    monkeypatch.setattr('sys.argv', ['remux_foreign.py', str(specfile), '--run', '--workers', '1'])
    rc = rf.main()
    gate.set()
    assert rc == 130
    # The single worker may already have dequeued F1 before the cancel lands; anything
    # beyond that must have been cancelled.
    assert started[0] == 'F0' and len(started) <= 2
    log = (tmp_path / 'logs' / 'remux_foreign.log').read_text()
    assert 'F5: not started' in log and 'F4: not started' in log


def test_worker_exception_is_reported_not_fatal(rf, monkeypatch, tmp_path):
    def fake_process(spec, apply_changes):
        if spec['folder'] == 'F1':
            raise RuntimeError('boom')
        return {'folder': spec['folder'], 'ok': True, 'size_gb': 1, 'seconds': 1}

    monkeypatch.setattr(rf, 'process', fake_process)
    specfile = tmp_path / 'spec.json'
    specfile.write_text(__import__('json').dumps(_specs(3)))
    monkeypatch.setattr('sys.argv', ['remux_foreign.py', str(specfile), '--run'])
    rc = rf.main()
    assert rc == 1
    log = (tmp_path / 'logs' / 'remux_foreign.log').read_text()
    assert 'F1: RuntimeError: boom' in log
    assert '2/3 succeeded' in log


def test_metadata_only_stops_between_films(rf, monkeypatch, tmp_path):
    seen = []

    def fake_refresh(spec):
        seen.append(spec['folder'])
        rf.shutdown_requested = True
        return {'folder': spec['folder'], 'ok': True, 'name_match': True}

    monkeypatch.setattr(rf, 'metadata_refresh', fake_refresh)
    specfile = tmp_path / 'spec.json'
    specfile.write_text(__import__('json').dumps(_specs(3)))
    monkeypatch.setattr('sys.argv', ['remux_foreign.py', str(specfile), '--metadata-only'])
    rc = rf.main()
    assert seen == ['F0']
    assert rc == 130


def test_clean_run_exit_zero(rf, monkeypatch, tmp_path):
    monkeypatch.setattr(rf, 'process', lambda spec, a: {'folder': spec['folder'], 'ok': True, 'dry_run': True})
    specfile = tmp_path / 'spec.json'
    specfile.write_text(__import__('json').dumps(_specs(2)))
    monkeypatch.setattr('sys.argv', ['remux_foreign.py', str(specfile)])
    assert rf.main() == 0
