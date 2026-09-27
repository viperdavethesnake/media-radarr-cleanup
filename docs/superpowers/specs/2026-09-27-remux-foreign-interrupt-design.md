# remux_foreign.py interrupt handling — design

Date: 2026-09-27. Item 8 from the 2026-09-26 doc-audit follow-ups.

## Problem

`foreign_cleanup/remux_foreign.py` installs a SIGINT/SIGTERM handler that sets
`shutdown_requested` and logs a line. Nothing reads the flag. After Ctrl-C:

- films already in mkvmerge run to completion (up to `REMUX_TIMEOUT` = 3600 s each);
- films still queued start as workers free up;
- a second Ctrl-C reaches Python's default handling and kills mkvmerge mid-write,
  leaving a partial file in `cleaned/<Film>/` that looks like output.

Separately, an mkvmerge failure or timeout today also leaves its partial output file in
place. Both cases feed the same hazard: the next manual step in this pipeline is
spot-check, then delete the source on David's instruction.

## Decision (David, 2026-09-27)

First signal terminates in-flight mkvmerge processes immediately and removes their
partial output. Speed over finishing what was started; interrupted films are simply
rerun later.

## Behaviour

### Signal handling

- First SIGINT or SIGTERM: set `shutdown_requested`, call `terminate()` on every live
  mkvmerge process, log one line. No timers.
- Second signal while cleanup is still running: `os._exit(130)`.

### `run_mkvmerge`

- Switches from `subprocess.run` to `subprocess.Popen` + `communicate(timeout=REMUX_TIMEOUT)`.
- Registers the `Popen` in a module-level set guarded by a lock for the duration of the
  call, and removes it afterwards (in `finally`).
- Exit code semantics unchanged: 0 ok, 1 warnings (output kept), ≥2 raises
  `CalledProcessError`. A terminated process exits non-zero and takes the ≥2 path.
- On `TimeoutExpired`, the process is killed before re-raising (it is not today).

### `process()`

- A new `_discard_partial(dst, dst_folder)` helper: unlink `dst` if it exists, `rmdir`
  `dst_folder` if it is now empty. Never touches other files in the folder (a previous
  run's output may live there).
- Called on the timeout path, the `CalledProcessError` path, and the generic exception
  path, but only once `dst` has been computed and `apply_changes` is true.
- If `shutdown_requested` is set when a failure is caught, the result is
  `{'folder', 'ok': False, 'error': 'interrupted', 'interrupted': True}` instead of the
  mkvmerge error text.

### Main loop (`--run` and dry-run)

- Check `shutdown_requested` on each completed future. When set: cancel every pending
  future, stop collecting, and record the cancelled films as `not started`.
- `fut.result()` is wrapped; an exception escaping `process()` becomes
  `{'folder', 'ok': False, 'error': f'{type(e).__name__}: {e}'}` rather than a crash.
- Summary groups: succeeded, failed, interrupted, not started (each film listed under
  one heading).
- Exit code: 130 if interrupted or any film not started; else 1 if any failed; else 0.

### `--metadata-only`

Sequential loop checks `shutdown_requested` before each film and stops; remaining films
are reported as not started. There are no child processes to kill on this path.

### Dry-run

Unchanged: no processes, no output, so no cleanup. The flag check still stops the loop.

## Out of scope

- Process groups / `os.killpg`: mkvmerge spawns no children.
- Changing `REMUX_TIMEOUT`, worker count, or the spec schema.
- The other foreign scripts (`inspect_foreign.py` already stops on the flag; the rest
  are sequential and short).

## Tests

`foreign_cleanup/tests/test_remux_interrupt.py`, using `sleep` as the stand-in mkvmerge
command and `RC_SERVARR_DIR` pointed at `tmp_path`:

1. `signal_handler` terminates a registered live process; the worker's partial output
   file is removed; the folder is removed when empty; a pre-existing sibling file in the
   same folder is left alone and the folder is then kept.
2. An mkvmerge non-zero exit (no signal) also removes the partial file and reports the
   mkvmerge error, not `interrupted`.
3. With the flag set after the first future completes, pending futures are cancelled and
   appear under `not started`; exit code is 130.
4. `--metadata-only` stops between films once the flag is set.
5. A worker exception escaping `process()` is reported as that film's error and the
   batch completes.

## Docs

- `foreign_cleanup/PIPELINE.md`: tools notes gain one line on Ctrl-C behaviour and the
  130 exit code.
- `README.md` maintenance/foreign line: none needed beyond PIPELINE.md.
