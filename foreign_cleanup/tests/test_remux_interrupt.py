"""Ctrl-C on remux_foreign kills in-flight mkvmerge, removes partial output, cancels the rest."""
import importlib
import os
import re
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
    # Flag flips right after mkvmerge finishes (e.g. during regen_metadata) — process()
    # already checked the flag on entry and run_mkvmerge already started, so neither
    # guard fires retroactively; a completed remux must still report ok.
    src, dst_folder = film
    monkeypatch.setattr(rf, 'build_cmd', _cmd_writing_partial_then(dst_folder, 0))
    real_run_mkvmerge = rf.run_mkvmerge

    def run_then_interrupt(cmd, label):
        real_run_mkvmerge(cmd, label)
        rf.shutdown_requested = True

    monkeypatch.setattr(rf, 'run_mkvmerge', run_then_interrupt)
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


# ── Fix round 1: never start a film after Ctrl-C ─────────────────────────────

def test_process_returns_not_started_when_flag_set_on_entry(rf, film, monkeypatch):
    src, dst_folder = film

    def must_not_be_called(path):
        raise AssertionError('mkv_identify should not be called when shutdown already requested')
    monkeypatch.setattr(rf, 'mkv_identify', must_not_be_called)
    rf.shutdown_requested = True
    r = rf.process(_spec(src), True)
    assert r == {'folder': 'Film_(2001)', 'ok': False, 'error': 'not started', 'not_started': True}


def test_run_mkvmerge_refuses_to_start_when_flag_set(rf, monkeypatch):
    # Belt-and-braces: if the fix under test is missing, run_mkvmerge would actually
    # start the sleep and block on communicate() for the real duration — cap the
    # timeout short so a regression fails fast instead of hanging the suite.
    monkeypatch.setattr(rf, 'REMUX_TIMEOUT', 2)
    rf.shutdown_requested = True
    with pytest.raises(InterruptedError):
        rf.run_mkvmerge(['sleep', '3109'], 'film')
    assert not rf._live_procs
    out = subprocess.run(['pgrep', '-f', '^sleep 3109$'], capture_output=True, text=True)
    assert out.stdout.strip() == '', 'sleep 3109 was started despite the flag being set'


def test_refusal_before_mkvmerge_keeps_existing_output_and_reports_not_started(rf, film, monkeypatch):
    # Ctrl-C lands after process()'s entry check but before mkvmerge spawns (during
    # mkv_identify/TMDB here) — a previously-completed output for this folder must
    # survive, and the film must be reported as not started, not interrupted.
    src, dst_folder = film
    dst_folder.mkdir(parents=True)
    existing = dst_folder / 'Film_(2001)_[1080p_hevc_truehd].mkv'
    existing.write_bytes(b'old')

    def identify_then_interrupt(path):
        rf.shutdown_requested = True
        return INFO
    monkeypatch.setattr(rf, 'mkv_identify', identify_then_interrupt)
    monkeypatch.setattr(rf, 'build_cmd', lambda spec, src, dst: ['sleep', '3111'])

    r = rf.process(_spec(src), True)
    assert r == {'folder': 'Film_(2001)', 'ok': False, 'error': 'not started', 'not_started': True}
    assert existing.exists()
    assert existing.read_bytes() == b'old'
    assert not rf._live_procs
    out = subprocess.run(['pgrep', '-f', '^sleep 3111$'], capture_output=True, text=True)
    assert out.stdout.strip() == '', 'sleep 3111 was started despite the flag being set'


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
    assert started == ['F0']
    log = (tmp_path / 'logs' / 'remux_foreign.log').read_text()
    assert 'F5: not started' in log and 'F4: not started' in log

    assert 'CancelledError' not in log

    # Every film must land in exactly one summary group — no duplicates, no double-counting.
    summary_lines = [l for l in log.splitlines() if any(e in l for e in ('✅', '❌', '⚠️', '⏸'))]
    for name in ('F0', 'F1', 'F2', 'F3', 'F4', 'F5'):
        occurrences = sum(1 for l in summary_lines if f'{name}:' in l)
        assert occurrences == 1, f'{name} appears in {occurrences} summary groups'

    tally_line = next(l for l in log.splitlines() if '/6 succeeded' in l)
    ok_n = int(re.search(r'(\d+)/6 succeeded', tally_line).group(1))
    failed_n = int(m.group(1)) if (m := re.search(r'(\d+) failed', tally_line)) else 0
    interrupted_n = int(m.group(1)) if (m := re.search(r'(\d+) interrupted', tally_line)) else 0
    not_started_n = int(m.group(1)) if (m := re.search(r'(\d+) not started', tally_line)) else 0
    assert ok_n + failed_n + interrupted_n + not_started_n == 6
    assert not_started_n == 5


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


def test_exit_130_whenever_flag_set(rf, monkeypatch, tmp_path):
    # Case 1: flag already set before main() even starts — every film ends up
    # not-started, whether via the pre-sweep cancel or via process()'s own entry
    # check (mirrored here since 'process' is replaced for this test).
    def fake_process_pre_set(spec, apply_changes):
        if rf.shutdown_requested:
            return {'folder': spec['folder'], 'ok': False, 'error': 'not started', 'not_started': True}
        return {'folder': spec['folder'], 'ok': True, 'dry_run': True}

    monkeypatch.setattr(rf, 'process', fake_process_pre_set)
    specfile1 = tmp_path / 'spec1.json'
    specfile1.write_text(__import__('json').dumps(_specs(3)))
    monkeypatch.setattr('sys.argv', ['remux_foreign.py', str(specfile1), '--run'])
    rf.shutdown_requested = True
    assert rf.main() == 130

    # Case 2: every film actually completes 'ok' — the flag only gets set as a side
    # effect while the last one is running. Exit must still be 130, not 0.
    rf.shutdown_requested = False

    def fake_process_late_set(spec, apply_changes):
        if spec['folder'] == 'F2':
            rf.shutdown_requested = True
        return {'folder': spec['folder'], 'ok': True, 'dry_run': True}

    monkeypatch.setattr(rf, 'process', fake_process_late_set)
    specfile2 = tmp_path / 'spec2.json'
    specfile2.write_text(__import__('json').dumps(_specs(3)))
    monkeypatch.setattr('sys.argv', ['remux_foreign.py', str(specfile2), '--run', '--workers', '1'])
    assert rf.main() == 130
