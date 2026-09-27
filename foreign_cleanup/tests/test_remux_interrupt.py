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
