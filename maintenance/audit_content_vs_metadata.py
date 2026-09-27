#!/usr/bin/env python3
"""
Audit movie content vs. metadata for mismatches.

For each processed movie folder this script:
  1. Reads the TMDB runtime from movie.nfo (<runtime> in minutes)
  2. Reads the actual video duration via ffprobe
  3. Compares them — flags if they differ by more than RUNTIME_TOLERANCE_MINUTES
  4. Reads the MKV TITLE tag and checks it matches the NFO <title>

A mismatch strongly suggests the video content doesn't match the metadata,
i.e. batch_cleaner.py used the wrong IMDb ID for that source file.

Usage:
    python3 maintenance/audit_content_vs_metadata.py
    python3 maintenance/audit_content_vs_metadata.py /storage/media/movies
    python3 maintenance/audit_content_vs_metadata.py /storage/media/movies --csv report.csv
    python3 maintenance/audit_content_vs_metadata.py /storage/media/movies --tolerance 10
"""

import os
import re
import sys
import csv
import json
import argparse
import subprocess
from pathlib import Path
from xml.etree.ElementTree import parse as et_parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

DEFAULT_SCAN_DIR = os.getenv('RC_VERIFY_SCAN_DIR', '/storage/media/movies')
FFPROBE_BIN = os.getenv('RC_FFPROBE_BIN', 'ffprobe')
MAX_WORKERS = 12
RUNTIME_TOLERANCE_MINUTES = 5  # Flag if actual vs. NFO runtime differ by more than this


# ── helpers ──────────────────────────────────────────────────────────────────

def find_mkv(folder: Path) -> Path | None:
    for f in folder.iterdir():
        if f.suffix.lower() == '.mkv' and not f.is_symlink():
            return f
    return None


def nfo_fields(nfo_path: Path) -> dict:
    """Extract title, runtime, imdbid from movie.nfo."""
    result = {'title': None, 'runtime': None, 'imdbid': None}
    try:
        tree = et_parse(nfo_path)
        root = tree.getroot()
        for field in ('title', 'imdbid'):
            el = root.find(field)
            if el is not None and el.text:
                result[field] = el.text.strip()
        el = root.find('runtime')
        if el is not None and el.text:
            try:
                result['runtime'] = int(el.text.strip())
            except ValueError:
                pass
    except Exception:
        pass
    return result


def ffprobe_duration_seconds(mkv_path: Path) -> float | None:
    """Return video duration in seconds via ffprobe."""
    try:
        cmd = [
            FFPROBE_BIN, '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            str(mkv_path),
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        duration = data.get('format', {}).get('duration')
        return float(duration) if duration else None
    except Exception:
        return None


def mkv_title_tag(mkv_path: Path) -> str | None:
    """Read the TITLE tag written into the MKV by mkvpropedit."""
    try:
        cmd = ['mkvinfo', str(mkv_path)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in out.stdout.splitlines():
            if '+ Name: TITLE' in line or '| + Name: TITLE' in line:
                # Next line has the value
                idx = out.stdout.splitlines().index(line)
                lines = out.stdout.splitlines()
                if idx + 1 < len(lines):
                    val_line = lines[idx + 1]
                    m = re.search(r'String: (.+)', val_line)
                    if m:
                        return m.group(1).strip()
    except Exception:
        pass
    return None


def normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


# ── per-folder check ──────────────────────────────────────────────────────────

def check_folder(folder: Path, tolerance: int) -> dict | None:
    nfo_path = folder / 'movie.nfo'
    if not nfo_path.exists():
        return {
            'folder': folder.name,
            'issue': 'NO NFO',
            'nfo_title': '',
            'nfo_runtime_min': '',
            'actual_runtime_min': '',
            'delta_min': '',
            'imdbid': '',
            'mkv_title_tag': '',
        }

    fields = nfo_fields(nfo_path)
    mkv = find_mkv(folder)

    if not mkv:
        return {
            'folder': folder.name,
            'issue': 'NO MKV',
            'nfo_title': fields['title'] or '',
            'nfo_runtime_min': fields['runtime'] or '',
            'actual_runtime_min': '',
            'delta_min': '',
            'imdbid': fields['imdbid'] or '',
            'mkv_title_tag': '',
        }

    issues = []

    # ── Runtime check ──
    nfo_runtime = fields['runtime']   # minutes (from TMDB)
    actual_secs = ffprobe_duration_seconds(mkv)
    actual_min = round(actual_secs / 60) if actual_secs else None
    delta = abs(actual_min - nfo_runtime) if (actual_min and nfo_runtime) else None

    if nfo_runtime is None:
        issues.append('NFO missing runtime')
    elif actual_min is None:
        issues.append('ffprobe failed')
    elif delta > tolerance:
        issues.append(f'runtime delta {delta} min (NFO={nfo_runtime}, actual={actual_min})')

    # ── MKV TITLE tag check ──
    tag_title = mkv_title_tag(mkv)
    nfo_title = fields['title']
    if tag_title and nfo_title:
        if normalize(tag_title) != normalize(nfo_title):
            issues.append(f'MKV tag TITLE "{tag_title}" != NFO title "{nfo_title}"')

    if not issues:
        return None  # All good

    return {
        'folder': folder.name,
        'issue': ' | '.join(issues),
        'nfo_title': nfo_title or '',
        'nfo_runtime_min': nfo_runtime or '',
        'actual_runtime_min': actual_min or '',
        'delta_min': delta if delta is not None else '',
        'imdbid': fields['imdbid'] or '',
        'mkv_title_tag': tag_title or '',
    }


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Audit video content vs. NFO metadata')
    parser.add_argument('scan_dir', nargs='?', default=DEFAULT_SCAN_DIR)
    parser.add_argument('--csv', metavar='FILE', help='Write flagged results to CSV')
    parser.add_argument('--tolerance', type=int, default=RUNTIME_TOLERANCE_MINUTES,
                        metavar='MINUTES',
                        help=f'Runtime mismatch tolerance in minutes (default {RUNTIME_TOLERANCE_MINUTES})')
    args = parser.parse_args()

    scan_dir = Path(args.scan_dir)
    if not scan_dir.is_dir():
        print(f'ERROR: not a directory: {scan_dir}')
        sys.exit(1)

    folders = sorted(f for f in scan_dir.iterdir() if f.is_dir() and not f.is_symlink())
    total = len(folders)
    print(f'Scanning {total} folders in {scan_dir}  (tolerance: ±{args.tolerance} min)\n')

    flagged = []
    completed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(check_folder, f, args.tolerance): f for f in folders}
        for future in as_completed(futures):
            completed += 1
            print(f'\r  {completed}/{total}', end='', flush=True)
            try:
                result = future.result()
            except Exception as e:
                result = {'folder': futures[future].name, 'issue': f'audit error: {type(e).__name__}: {e}',
                          'nfo_title': '', 'nfo_runtime_min': '', 'actual_runtime_min': '',
                          'delta_min': '', 'imdbid': '', 'mkv_title_tag': ''}
            if result:
                flagged.append(result)

    print(f'\r  {total}/{total} done\n')

    if not flagged:
        print(f'✅ All {total} movies look clean — no content/metadata mismatches detected.')
        return

    flagged.sort(key=lambda r: r['folder'])

    print(f'⚠️  {len(flagged)} potential mismatch(es) out of {total} movies:\n')
    print(f'{"Folder":45}  {"Issue":<55}  IMDb ID')
    print('─' * 115)
    for m in flagged:
        print(f'{m["folder"][:45]:45}  {m["issue"][:55]:<55}  {m["imdbid"]}')

    if args.csv:
        fieldnames = ['folder', 'issue', 'nfo_title', 'nfo_runtime_min',
                      'actual_runtime_min', 'delta_min', 'imdbid', 'mkv_title_tag']
        with open(args.csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(flagged)
        print(f'\nCSV written to: {args.csv}')


if __name__ == '__main__':
    main()
