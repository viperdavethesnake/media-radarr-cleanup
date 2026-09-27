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
