# remux_foreign Interrupt Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Ctrl-C on `foreign_cleanup/remux_foreign.py` actually stop the batch: kill in-flight mkvmerge, remove partial output, cancel queued films, exit 130.

**Architecture:** `run_mkvmerge` switches to `Popen` and registers the process in a lock-guarded module set; the signal handler terminates every registered process. `process()` gains a partial-output cleanup used on every failure path. The main loop checks the flag per completed future, cancels the rest, and reports four groups. Same pattern `maintenance/audit_decode_integrity.py` already uses.

**Tech Stack:** Python 3.14, `subprocess.Popen`, `threading.Lock`, `concurrent.futures`, pytest. Tests use `sleep` as the stand-in for mkvmerge.

**Spec:** `docs/superpowers/specs/2026-09-27-remux-foreign-interrupt-design.md`

## Global Constraints

- Only `foreign_cleanup/remux_foreign.py`, a new `foreign_cleanup/tests/test_remux_interrupt.py`, and `foreign_cleanup/PIPELINE.md` change.
- First signal: terminate live mkvmerge processes, log once, no timers. Second signal: `os._exit(130)`.
- Cleanup removes only the file this run was writing (`dst`) and `rmdir`s `dst_folder` only if empty. Never delete other files in the folder.
- Exit codes: 130 if interrupted or any film not started; else 1 if any failed; else 0. `--metadata-only` follows the same rule.
- Exit-code semantics of mkvmerge unchanged: 0 ok, 1 warnings (output kept), ≥2 raises `CalledProcessError`.
- `REMUX_TIMEOUT`, worker default (3), spec schema, dry-run output: unchanged.
- Tests import the module as `foreign_cleanup.remux_foreign` (real module object, so `monkeypatch.setattr` works) with `RC_SERVARR_DIR` set to `tmp_path` and `importlib.reload` applied, the way `tv_cleanup/tests/test_remux_failures.py` does. `TMDB_API_KEY` must be set to any value before import.
- Test command for every task: `cd /home/david/git/radarr_cleanup && source .venv/bin/activate && python -m pytest foreign_cleanup/tests -q`.
- No push.

## Review Focus

1. Ctrl-C arrives before any future has completed (all workers mid-remux): the flag check runs only on completion, so the first completion after the kill must still cancel the rest. Task 3's cancel test sets the flag before the first `fut.result()` is read.
2. Ctrl-C during `regen_metadata` (after mkvmerge finished): there is no live process to kill; the film should finish and be reported as succeeded, not interrupted. Task 2's `interrupted` result is only produced on the failure paths, and Task 2 adds a test that a completed film with the flag set is still `ok`.
3. A film whose `dst_folder` already contains an earlier run's output: cleanup must leave that file. Task 2 tests it.
4. Second Ctrl-C while `terminate()` is still waiting on a slow process: must not hang. Task 1's handler calls `os._exit(130)` when the flag is already set; Task 1 tests that the second call exits.
5. `TimeoutExpired`: the process must be killed, not left running after the worker gives up. Task 1 tests it with a tiny timeout.

---

### Task 1: Live-process registry, terminating signal handler, Popen-based `run_mkvmerge`

**Files:**
- Modify: `foreign_cleanup/remux_foreign.py:13-14` (imports), `:31-41` (handler), `:176-182` (`run_mkvmerge`)
- Create: `foreign_cleanup/tests/test_remux_interrupt.py`

**Interfaces:**
- Produces: module globals `_live_procs: set[subprocess.Popen]`, `_live_lock: threading.Lock`, `shutdown_requested: bool`; `signal_handler(signum, frame)`; `run_mkvmerge(cmd, label)` with the same signature and raise behaviour as today. Tasks 2 and 3 rely on `shutdown_requested` and on `run_mkvmerge` raising `CalledProcessError` for a terminated process.

- [ ] **Step 1: Write the failing tests**

Create `foreign_cleanup/tests/test_remux_interrupt.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest foreign_cleanup/tests/test_remux_interrupt.py -q`
Expected: 3 FAIL (`_live_procs` AttributeError on the first two; the timeout test fails on the `pgrep` assertion or on `_live_procs`), 2 PASS (the warning/fatal tests already hold). Note which ones passed: those two are regression guards for Step 3.

- [ ] **Step 3: Implement**

In `foreign_cleanup/remux_foreign.py`, change the import line:

```python
import os, sys, json, time, shutil, signal, argparse, subprocess, threading, traceback
```

Replace the handler block (`shutdown_requested = False` through the two `signal.signal(...)` lines) with:

```python
shutdown_requested = False
_live_procs = set()          # mkvmerge processes currently running
_live_lock = threading.Lock()


def signal_handler(signum, frame):
    global shutdown_requested
    if shutdown_requested:
        os._exit(130)        # second signal: do not wait for cleanup
    shutdown_requested = True
    with _live_lock:
        procs = list(_live_procs)
    for p in procs:
        try:
            p.terminate()
        except OSError:
            pass
    log(f"⚠️ Interrupt received — terminated {len(procs)} mkvmerge process(es), "
        f"partial output will be removed, queued films cancelled")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
```

Replace `run_mkvmerge`:

```python
def run_mkvmerge(cmd, label):
    # 0 = ok, 1 = warnings (output still written), >=2 = fatal.
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with _live_lock:
        _live_procs.add(p)
    try:
        try:
            out, err = p.communicate(timeout=REMUX_TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            raise
    finally:
        with _live_lock:
            _live_procs.discard(p)
    if p.returncode == 1:
        log(f"⚠️ {label}: mkvmerge warnings (output kept): {(out or '').strip()[:400]}")
    elif p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, output=out, stderr=err)
```

`returncode != 0` rather than `>= 2` so a signal-killed process (negative code) also raises.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest foreign_cleanup/tests -q`
Expected: all pass (the 5 new plus the 4 existing spec-schema tests).

- [ ] **Step 5: Commit**

```bash
git add foreign_cleanup/remux_foreign.py foreign_cleanup/tests/test_remux_interrupt.py
git commit -m "feat(foreign): signal handler terminates live mkvmerge; Popen-based run_mkvmerge

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Partial-output cleanup and `interrupted` result in `process()`

**Files:**
- Modify: `foreign_cleanup/remux_foreign.py` `process()` (currently lines ~200-266)
- Test: `foreign_cleanup/tests/test_remux_interrupt.py`

**Interfaces:**
- Consumes: `shutdown_requested`, `run_mkvmerge` from Task 1.
- Produces: `_discard_partial(dst: str, dst_folder: str) -> None`; `process(spec, apply_changes)` returns, on interrupt, `{'folder': str, 'ok': False, 'error': 'interrupted', 'interrupted': True}`. Task 3 groups on the `interrupted` key.

- [ ] **Step 1: Write the failing tests**

Append to `foreign_cleanup/tests/test_remux_interrupt.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest foreign_cleanup/tests/test_remux_interrupt.py -q`
Expected: the four cleanup/interrupt tests FAIL (partial file still exists; interrupted result is the mkvmerge error text). `test_flag_set_after_success_is_still_ok` may already pass; that is fine, it is a guard.

- [ ] **Step 3: Implement**

Add above `process()`:

```python
def _discard_partial(dst, dst_folder):
    """Remove the file this run was writing; drop the folder only if it is now empty."""
    try:
        if dst and os.path.exists(dst):
            os.remove(dst)
        if dst_folder and os.path.isdir(dst_folder) and not os.listdir(dst_folder):
            os.rmdir(dst_folder)
    except OSError as e:
        log(f"  ⚠️ could not remove partial output {dst}: {e}")
```

In `process()`: initialise `dst = dst_folder = None` right after `t0 = time.perf_counter()`, so the except blocks can reference them. Then replace the three `except` blocks at the end of `process()` with:

```python
    except subprocess.TimeoutExpired:
        _discard_partial(dst, dst_folder)
        return {'folder': label, 'ok': False,
                'error': f'mkvmerge exceeded {REMUX_TIMEOUT}s — investigate pool health, do not simply retry'}
    except subprocess.CalledProcessError as e:
        _discard_partial(dst, dst_folder)
        if shutdown_requested:
            return {'folder': label, 'ok': False, 'error': 'interrupted', 'interrupted': True}
        detail = ((e.output or '') + (e.stderr or '')).strip()[:400]
        return {'folder': label, 'ok': False, 'error': f'mkvmerge failed: {detail}'}
    except Exception as e:
        _discard_partial(dst, dst_folder)
        if shutdown_requested:
            return {'folder': label, 'ok': False, 'error': 'interrupted', 'interrupted': True}
        log(f"❌ {label}: {traceback.format_exc()}")
        return {'folder': label, 'ok': False, 'error': str(e)}
```

`_discard_partial` is safe on the dry-run and early-return paths because `dst` is `None` there.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest foreign_cleanup/tests -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add foreign_cleanup/remux_foreign.py foreign_cleanup/tests/test_remux_interrupt.py
git commit -m "feat(foreign): remove partial output on failure/timeout/interrupt in remux_foreign

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Main loop cancels pending work, grouped summary, exit 130; `--metadata-only` stops on the flag

**Files:**
- Modify: `foreign_cleanup/remux_foreign.py` `main()` (currently lines ~297-345)
- Modify: `foreign_cleanup/PIPELINE.md` (tools notes)
- Test: `foreign_cleanup/tests/test_remux_interrupt.py`

**Interfaces:**
- Consumes: `process()` results with the optional `interrupted` key (Task 2), `shutdown_requested` (Task 1), `metadata_refresh(spec)` (existing).
- Produces: `_summarise(results, not_started) -> int` (exit code), used by both paths of `main()`.

- [ ] **Step 1: Write the failing tests**

Append to `foreign_cleanup/tests/test_remux_interrupt.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest foreign_cleanup/tests/test_remux_interrupt.py -q`
Expected: `test_pending_films_cancelled_after_interrupt` FAILS (rc 1, all six started); `test_worker_exception_is_reported_not_fatal` FAILS with `RuntimeError: boom` escaping; `test_metadata_only_stops_between_films` FAILS (`seen` has three entries). `test_clean_run_exit_zero` passes already.

- [ ] **Step 3: Implement**

Add above `main()`:

```python
def _summarise(results, not_started):
    """Log the four groups and return the exit code."""
    print()
    ok = [r for r in results if r['ok']]
    interrupted = [r for r in results if r.get('interrupted')]
    failed = [r for r in results if not r['ok'] and not r.get('interrupted')]
    for r in sorted(ok, key=lambda x: x['folder']):
        if 'name_match' in r:
            extra = 'name OK' if r['name_match'] else f"NAME MISMATCH: have {r['filename']!r}, computed {r['computed']!r}"
        elif r.get('dry_run'):
            extra = '(dry-run)'
        else:
            extra = f"{r.get('size_gb')} GB in {r.get('seconds')}s"
        log(f"✅ {r['folder']}: {extra}")
    for r in sorted(failed, key=lambda x: x['folder']):
        log(f"❌ {r['folder']}: {r['error']}")
    for r in sorted(interrupted, key=lambda x: x['folder']):
        log(f"⚠️ {r['folder']}: interrupted (partial output removed)")
    for name in sorted(not_started):
        log(f"⏸ {name}: not started")
    log(f"{len(ok)}/{len(results) + len(not_started)} succeeded"
        + (f", {len(failed)} failed" if failed else "")
        + (f", {len(interrupted)} interrupted" if interrupted else "")
        + (f", {len(not_started)} not started" if not_started else ""))
    if interrupted or not_started:
        return 130
    return 1 if failed else 0
```

Replace `main()` from `if args.metadata_only:` to the end of the function with:

```python
    if args.metadata_only:
        log(f"▶ metadata refresh: {len(specs)} film(s) in {CLEANED_DIR}")
        results, not_started = [], []
        for s in specs:
            if shutdown_requested:
                not_started.append(s['folder'])
                continue
            results.append(metadata_refresh(s))
        return _summarise(results, not_started)

    log(f"▶ {len(specs)} film(s), {args.workers} concurrent, "
        f"timeout {REMUX_TIMEOUT}s each {'[RUN]' if args.run else '[DRY-RUN]'}")

    results, not_started = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, s, args.run): s['folder'] for s in specs}
        for fut in as_completed(futures):
            if shutdown_requested:
                for f, name in futures.items():
                    if f.cancel():
                        not_started.append(name)
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({'folder': futures[fut], 'ok': False,
                                'error': f'{type(e).__name__}: {e}'})
            if shutdown_requested:
                # Remaining futures are either cancelled or already running (and being
                # killed by the handler); collect those as they finish.
                continue
    return _summarise(results, not_started)
```

Note on the `as_completed` loop: once the flag is set, cancelled futures never appear in `as_completed`, and the in-flight ones return their `interrupted` result after the handler terminated them. The `continue` is explicit so a reader sees the loop is meant to drain.

In `foreign_cleanup/PIPELINE.md`, in the `Notes:` list under `## Tools`, add after the `remux_foreign.py` validation bullet:

```
- Ctrl-C (or SIGTERM) on `remux_foreign.py --run` terminates every running mkvmerge,
  removes their partial output, cancels queued films, and exits 130 with a summary of
  succeeded / failed / interrupted / not started. Rerun the same spec to pick up the
  rest. A second Ctrl-C hard-exits. A failed or timed-out mkvmerge also has its partial
  output removed.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest foreign_cleanup/tests -q`
Expected: all pass. Then the full suite:
`python -m pytest maintenance/tests movie_cleanup/tests foreign_cleanup/tests music_cleanup/tests tv_cleanup/tests -q`
Expected: all pass (54 existing + the new ones).

- [ ] **Step 5: Smoke test the real script**

```bash
cd /home/david/git/radarr_cleanup && source .venv/bin/activate
python3 -W ignore foreign_cleanup/remux_foreign.py --help | head -3
python3 -W ignore foreign_cleanup/remux_foreign.py foreign_cleanup/spec_batch4.json; echo "exit=$?"
```
Expected: help prints; the dry-run either prints `[DRY-RUN]` lines or per-film spec-mismatch errors (the source folders are gone), and the exit code is 0 or 1, never a traceback.

- [ ] **Step 6: Commit**

```bash
git add foreign_cleanup/remux_foreign.py foreign_cleanup/tests/test_remux_interrupt.py foreign_cleanup/PIPELINE.md
git commit -m "feat(foreign): Ctrl-C cancels queued films, grouped summary, exit 130

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
