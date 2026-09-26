#!/usr/bin/env python3
"""
Movie decode-integrity audit.

For every MKV in the scan directory, runs ffmpeg full-decode of the primary
video stream and captures any decoder errors / warnings. Catches bitstream
corruption that ffprobe does not surface:

  - "First slice in a frame missing"  (HEVC slice corruption)
  - "mmco: ..."                       (H.264 reference frame corruption)
  - "decode_slice_header error"
  - "non-existing PPS / SPS"
  - "concealing N DC, ... errors"
  - "Invalid NAL unit ..."
  - generic "error while decoding" / "corrupt" messages

CPU decode is used deliberately — NVDEC silently conceals slice / reference
errors. Software decode is the sensitive path.

Outputs (under $RC_SERVARR_DIR/logs/, default /storage/media/servarr/logs/):
  - movie_decode_audit_<ts>.log    incremental, human-readable
  - movie_decode_audit_<ts>.jsonl  one JSON result per line, append-as-you-go
  - movie_decode_audit_<ts>.md     markdown summary, written at end

The audit is a multi-day run on a large library. SIGINT/SIGTERM is honored:
in-flight ffmpeg processes are terminated, the markdown summary is written
for whatever finished, and the process exits cleanly. To pick up where a
prior run left off, pass that run's JSONL via --resume; files already
recorded with status OK / ERRORS / TIMEOUT are skipped, while FAILED /
INTERRUPTED are retried.

Default concurrency (--workers 3 --threads 4 = 12 of 20 logical cores on
the production box) is chosen to leave Jellyfin transcoding + Frigate +
other co-resident workloads with adequate CPU headroom. Push higher only
when the box is otherwise idle.

Usage:
    python3 maintenance/audit_decode_integrity.py
    python3 maintenance/audit_decode_integrity.py /storage/media/movies
    python3 maintenance/audit_decode_integrity.py --limit 5
    python3 maintenance/audit_decode_integrity.py --workers 3 --threads 4
    python3 maintenance/audit_decode_integrity.py --resume /storage/media/servarr/logs/movie_decode_audit_<ts>.jsonl
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── config ───────────────────────────────────────────────────────────────────
DEFAULT_SCAN_DIR = os.getenv('RC_VERIFY_SCAN_DIR', '/storage/media/movies')
FFMPEG_BIN       = os.getenv('RC_FFMPEG_BIN', 'ffmpeg')
FFPROBE_BIN      = os.getenv('RC_FFPROBE_BIN', 'ffprobe')

# 3 parallel ffmpeg processes × 4 threads each = 12 of 20 logical cores on
# the i7-12700KF. Leaves headroom for Jellyfin (demux / audio / subs around
# NVENC), Frigate, Ollama + OS. Matches the docstring and CLAUDE.md.
DEFAULT_WORKERS  = 3
DEFAULT_THREADS  = 4

# Absolute per-file wallclock cap. Real-world worst-case is a 4-hour 4K HEVC
# decoding ~0.5x realtime under contention → ~8h. Cap higher to be safe; the
# user can SIGINT the run if a single file truly hangs.
PER_FILE_TIMEOUT = 12 * 60 * 60   # 12 hours

SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
LOG_DIR = Path(SERVARR_DIR) / 'logs'


# ── shutdown / live process tracking ─────────────────────────────────────────
shutdown_requested = False
_live_procs: set[subprocess.Popen] = set()
_live_procs_lock = threading.Lock()


def _signal_handler(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    # Kill any running ffmpeg children so workers don't hold the pool open.
    with _live_procs_lock:
        for p in list(_live_procs):
            try:
                p.terminate()
            except Exception:
                pass


signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ── error classification ─────────────────────────────────────────────────────
# Lines matching BENIGN are dropped before reporting. Everything else from
# ffmpeg's stderr is kept (we err on the side of surfacing). HIGH_PRIORITY
# patterns are additionally tagged so the markdown summary highlights the
# most actionable corruption classes.

BENIGN = [
    re.compile(r'^\s*$'),
    re.compile(r'^Last message repeated \d+ times'),
    re.compile(r'Application provided invalid, non monotonically increasing dts'),
    re.compile(r'deprecated pixel format used'),
    re.compile(r'changing dts from \S+ to \S+'),
    re.compile(r'Auto-inserting'),
    re.compile(r'frame=\s*\d+'),
    re.compile(r'\[matroska,webm @ 0x[0-9a-f]+\] Estimating duration'),
    # Dolby Vision RPU / enhancement-layer Block Addition Mapping (hvcE).
    # ffmpeg warns when it sees an unknown BAM type, but the video stream
    # itself decodes fine — pure container-metadata warning, not corruption.
    re.compile(r'Invalid Block Addition value .* for unknown Block Addition Mapping type'),
    re.compile(r'Stream #\d+:\d+ -> #\d+:\d+'),
    re.compile(r'Guessed Channel Layout'),
    re.compile(r'aac bitstream not in ADTS'),
    re.compile(r'^Output #'),
    re.compile(r'^Input #'),
    re.compile(r'^Press \[q\]'),
    re.compile(r'^\s*Stream mapping:'),
]

HIGH_PRIORITY = [
    (re.compile(r'First slice in a frame missing', re.I),     'hevc_slice_missing'),
    (re.compile(r'\bmmco\b', re.I),                           'h264_mmco'),
    (re.compile(r'reference picture missing',     re.I),      'missing_reference'),
    (re.compile(r'decode_slice_header error',     re.I),      'slice_header_error'),
    (re.compile(r'non[- ]existing PPS',           re.I),      'missing_pps'),
    (re.compile(r'non[- ]existing SPS',           re.I),      'missing_sps'),
    (re.compile(r'concealing.*errors',            re.I),      'concealment'),
    (re.compile(r'\b(corrupt|damaged)\b',         re.I),      'corrupt_data'),
    (re.compile(r'Invalid NAL',                   re.I),      'invalid_nal'),
    (re.compile(r'cabac decode of mb',            re.I),      'cabac_decode_error'),
    (re.compile(r'error while decoding',          re.I),      'decode_error'),
    (re.compile(r'co located POCs',               re.I),      'co_located_pocs'),
    (re.compile(r'Invalid level prefix',          re.I),      'invalid_level_prefix'),
]


def is_benign(line: str) -> bool:
    return any(p.search(line) for p in BENIGN)


def classify(line: str) -> Optional[str]:
    for pat, tag in HIGH_PRIORITY:
        if pat.search(line):
            return tag
    return None


def collapse(lines: list[str]) -> list[str]:
    """Collapse runs of identical lines to 'line  (×N)'."""
    out: list[str] = []
    for ln in lines:
        if out and out[-1].split('  (×')[0] == ln:
            base = out[-1].split('  (×')[0]
            m = re.search(r'\(×(\d+)\)$', out[-1])
            n = int(m.group(1)) + 1 if m else 2
            out[-1] = f'{base}  (×{n})'
        else:
            out.append(ln)
    return out


# ── result ───────────────────────────────────────────────────────────────────
@dataclass
class DecodeResult:
    folder:           str
    mkv:              str
    path:             str
    duration_sec:     Optional[float]
    decode_time_sec:  float
    return_code:      int
    status:           str            # OK | ERRORS | TIMEOUT | FAILED | INTERRUPTED
    tags:             list[str]      = field(default_factory=list)
    errors:           list[str]      = field(default_factory=list)
    error_count_raw:  int            = 0
    note:             str            = ''


# ── ffprobe helper ───────────────────────────────────────────────────────────
def probe_duration(mkv: Path) -> Optional[float]:
    try:
        r = subprocess.run(
            [FFPROBE_BIN, '-v', 'quiet', '-print_format', 'json',
             '-show_format', str(mkv)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return None
        d = json.loads(r.stdout).get('format', {}).get('duration')
        return float(d) if d else None
    except Exception:
        return None


# ── decode check ─────────────────────────────────────────────────────────────
def decode_check(mkv: Path, threads: int) -> DecodeResult:
    folder = mkv.parent.name
    result = DecodeResult(
        folder=folder, mkv=mkv.name, path=str(mkv),
        duration_sec=None, decode_time_sec=0.0,
        return_code=-1, status='FAILED',
    )

    if shutdown_requested:
        result.status = 'INTERRUPTED'
        return result

    result.duration_sec = probe_duration(mkv)

    cmd = [
        FFMPEG_BIN, '-nostdin', '-hide_banner', '-v', 'warning',
        '-threads', str(threads),
        '-i', str(mkv),
        '-map', '0:v:0',
        '-f', 'null', '-',
    ]

    t0 = time.perf_counter()
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, errors='replace',
        )
        with _live_procs_lock:
            _live_procs.add(proc)

        try:
            _, stderr = proc.communicate(timeout=PER_FILE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
            result.status = 'TIMEOUT'
            result.note  = f'exceeded {PER_FILE_TIMEOUT}s cap'

        result.return_code     = proc.returncode
        result.decode_time_sec = time.perf_counter() - t0

        kept: list[str] = []
        tags: Counter[str] = Counter()
        raw  = 0
        for ln in (stderr or '').splitlines():
            ln = ln.rstrip()
            if not ln or is_benign(ln):
                continue
            raw += 1
            kept.append(ln)
            tag = classify(ln)
            if tag:
                tags[tag] += 1

        result.errors          = collapse(kept)
        result.error_count_raw = raw
        result.tags            = sorted(tags)

        if result.status == 'TIMEOUT':
            pass
        elif shutdown_requested and proc.returncode != 0:
            result.status = 'INTERRUPTED'
        elif raw == 0 and proc.returncode == 0:
            result.status = 'OK'
        elif raw > 0:
            result.status = 'ERRORS'
        else:
            result.status = 'FAILED'
            result.note   = f'ffmpeg exited {proc.returncode} with no parsed errors'

    except Exception as e:
        result.decode_time_sec = time.perf_counter() - t0
        result.status = 'FAILED'
        result.note   = f'{type(e).__name__}: {e}'
    finally:
        if proc is not None:
            with _live_procs_lock:
                _live_procs.discard(proc)

    return result


# ── incremental writer ───────────────────────────────────────────────────────
class IncrementalLog:
    """Thread-safe writer for the per-file log and JSONL stream."""

    def __init__(self, log_path: Path, jsonl_path: Path):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path   = log_path
        self.jsonl_path = jsonl_path
        self.lock       = threading.Lock()
        self._log_fh    = open(log_path,   'a', encoding='utf-8')
        self._jsonl_fh  = open(jsonl_path, 'a', encoding='utf-8')

    def msg(self, line: str) -> None:
        ts = time.strftime('[%Y-%m-%d %H:%M:%S]')
        out = f'{ts} {line}'
        with self.lock:
            print(out, flush=True)
            self._log_fh.write(out + '\n')
            self._log_fh.flush()

    def record(self, r: DecodeResult) -> None:
        icon = {
            'OK':          '✅',
            'ERRORS':      '🔴',
            'TIMEOUT':     '⏱️',
            'FAILED':      '❌',
            'INTERRUPTED': '⚠️',
        }.get(r.status, '?')
        head = (f'{icon} {r.status:11s} {r.folder}  '
                f'({r.decode_time_sec:.1f}s, {r.error_count_raw} err lines, '
                f'tags={",".join(r.tags) or "—"})')
        with self.lock:
            print(head, flush=True)
            self._log_fh.write(time.strftime('[%Y-%m-%d %H:%M:%S] ') + head + '\n')
            for ln in r.errors[:50]:
                self._log_fh.write(f'    | {ln}\n')
            if len(r.errors) > 50:
                self._log_fh.write(f'    | ... {len(r.errors)-50} more lines (see JSONL)\n')
            if r.note:
                self._log_fh.write(f'    note: {r.note}\n')
            self._log_fh.flush()
            self._jsonl_fh.write(json.dumps(asdict(r), ensure_ascii=False) + '\n')
            self._jsonl_fh.flush()

    def close(self) -> None:
        with self.lock:
            self._log_fh.close()
            self._jsonl_fh.close()


# ── markdown summary ─────────────────────────────────────────────────────────
def write_markdown(results: list[DecodeResult], scan_dir: Path,
                   started: datetime, finished: datetime, md_path: Path) -> None:
    by_status = Counter(r.status for r in results)
    tag_counts: Counter[str] = Counter()
    for r in results:
        tag_counts.update(r.tags)

    flagged = [r for r in results if r.status != 'OK']
    errors  = [r for r in results if r.status == 'ERRORS']
    timeouts = [r for r in results if r.status == 'TIMEOUT']
    failed   = [r for r in results if r.status == 'FAILED']

    L: list[str] = []
    a = L.append

    a(f'# Movie Decode-Integrity Audit')
    a('')
    a(f'**Library:** `{scan_dir}`  ')
    a(f'**Started:**  {started.isoformat(timespec="seconds")}  ')
    a(f'**Finished:** {finished.isoformat(timespec="seconds")}  ')
    a(f'**Elapsed:**  {(finished - started)}  ')
    a(f'**Scanned:**  {len(results)} files')
    a('')

    a('## Summary')
    a('')
    a('| Status | Count |')
    a('|---|---|')
    for s in ('OK', 'ERRORS', 'TIMEOUT', 'FAILED', 'INTERRUPTED'):
        a(f'| {s} | {by_status.get(s, 0)} |')
    a('')

    if tag_counts:
        a('## Corruption Classes')
        a('')
        a('| Tag | Files |')
        a('|---|---|')
        for tag, n in tag_counts.most_common():
            a(f'| `{tag}` | {n} |')
        a('')

    def detail_table(title: str, rows: list[DecodeResult]) -> None:
        if not rows:
            return
        a(f'## {title}')
        a('')
        a('| Folder | Tags | Err Lines | Decode (s) | First Error |')
        a('|---|---|---|---|---|')
        for r in sorted(rows, key=lambda x: (-x.error_count_raw, x.folder)):
            first = (r.errors[0] if r.errors else (r.note or '—'))
            first = first.replace('|', '\\|')
            if len(first) > 120:
                first = first[:117] + '...'
            a(f'| `{r.folder}` | {",".join(r.tags) or "—"} | '
              f'{r.error_count_raw} | {r.decode_time_sec:.1f} | {first} |')
        a('')

    detail_table('🔴 Decode Errors', errors)
    detail_table('⏱️ Timeouts',      timeouts)
    detail_table('❌ Failed to Run', failed)

    a('---')
    a(f'*Generated by `maintenance/audit_decode_integrity.py`*')

    md_path.write_text('\n'.join(L), encoding='utf-8')


# ── main ─────────────────────────────────────────────────────────────────────
def find_mkvs(scan_dir: Path) -> list[Path]:
    mkvs: list[Path] = []
    for entry in sorted(scan_dir.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        for f in sorted(entry.iterdir()):
            if f.is_symlink():
                continue
            if f.suffix.lower() == '.mkv' and f.is_file():
                mkvs.append(f)
    return mkvs


def main() -> int:
    ap = argparse.ArgumentParser(description='Full-decode CPU audit of every MKV in a movie library')
    ap.add_argument('scan_dir', nargs='?', default=DEFAULT_SCAN_DIR)
    ap.add_argument('--workers', type=int, default=DEFAULT_WORKERS,
                    help=f'Parallel ffmpeg processes (default {DEFAULT_WORKERS})')
    ap.add_argument('--threads', type=int, default=DEFAULT_THREADS,
                    help=f'ffmpeg -threads per process (default {DEFAULT_THREADS})')
    ap.add_argument('--limit', type=int, default=0,
                    help='Process only the first N MKVs (for testing)')
    ap.add_argument('--resume', metavar='JSONL', action='append',
                    help='Skip MKVs already recorded in this prior JSONL '
                         '(OK/ERRORS/TIMEOUT skipped; FAILED/INTERRUPTED retried). '
                         'Repeatable — after multiple interruptions, pass every '
                         'prior JSONL so all completed files are skipped.')
    args = ap.parse_args()

    scan_dir = Path(args.scan_dir)
    if not scan_dir.is_dir():
        print(f'ERROR: not a directory: {scan_dir}', file=sys.stderr)
        return 1

    mkvs = find_mkvs(scan_dir)
    total_before_resume = len(mkvs)

    skipped_by_resume = 0
    if args.resume:
        skip_paths: set[str] = set()
        skip_statuses = {'OK', 'ERRORS', 'TIMEOUT'}
        for resume_file in args.resume:
            resume_path = Path(resume_file)
            if not resume_path.is_file():
                print(f'ERROR: --resume file not found: {resume_path}', file=sys.stderr)
                return 1
            with open(resume_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get('status') in skip_statuses and rec.get('path'):
                        # Normalize so a relative-vs-absolute scan_dir spelling
                        # between runs still matches.
                        skip_paths.add(str(Path(rec['path']).resolve()))
        before = len(mkvs)
        mkvs = [m for m in mkvs if str(m.resolve()) not in skip_paths]
        skipped_by_resume = before - len(mkvs)

    if args.limit > 0:
        mkvs = mkvs[:args.limit]

    if not mkvs:
        if skipped_by_resume:
            print(f'No remaining MKVs — {skipped_by_resume} already completed in {", ".join(args.resume)}')
        else:
            print(f'No MKVs found in {scan_dir}')
        return 0

    ts = time.strftime('%Y%m%d_%H%M%S')
    log_path   = LOG_DIR / f'movie_decode_audit_{ts}.log'
    jsonl_path = LOG_DIR / f'movie_decode_audit_{ts}.jsonl'
    md_path    = LOG_DIR / f'movie_decode_audit_{ts}.md'

    log = IncrementalLog(log_path, jsonl_path)
    started = datetime.now()

    log.msg(f'Decode-integrity audit starting')
    log.msg(f'  scan_dir = {scan_dir}')
    if args.resume:
        log.msg(f'  resume   = {", ".join(args.resume)}  (skipping {skipped_by_resume} '
                f'of {total_before_resume} already-completed)')
    log.msg(f'  files    = {len(mkvs)}')
    log.msg(f'  workers  = {args.workers} × threads {args.threads} '
            f'= {args.workers * args.threads} cores nominal')
    log.msg(f'  log      = {log_path}')
    log.msg(f'  jsonl    = {jsonl_path}')

    results: list[DecodeResult] = []
    completed = 0
    total = len(mkvs)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(decode_check, m, args.threads): m for m in mkvs}
            for fut in as_completed(futures):
                completed += 1
                try:
                    r = fut.result()
                except Exception as e:
                    mkv = futures[fut]
                    r = DecodeResult(
                        folder=mkv.parent.name, mkv=mkv.name, path=str(mkv),
                        duration_sec=None, decode_time_sec=0.0,
                        return_code=-1, status='FAILED',
                        note=f'worker raised {type(e).__name__}: {e}',
                    )
                results.append(r)
                log.record(r)
                log.msg(f'  progress {completed}/{total}')
                if shutdown_requested:
                    # Don't submit more; drain in-flight and bail.
                    for f in futures:
                        if not f.done():
                            f.cancel()
                    break
    finally:
        finished = datetime.now()
        log.msg(f'Audit finished — writing markdown summary to {md_path}')
        write_markdown(results, scan_dir, started, finished, md_path)
        log.msg(f'Summary: ' + ', '.join(
            f'{s}={sum(1 for r in results if r.status == s)}'
            for s in ('OK', 'ERRORS', 'TIMEOUT', 'FAILED', 'INTERRUPTED')
        ))
        log.close()

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
