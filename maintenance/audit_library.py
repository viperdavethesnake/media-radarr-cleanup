#!/usr/bin/env python3
"""
Comprehensive movie library audit.

Checks per movie folder:
  1.  Folder name ↔ MKV filename match
  2.  NFO title + year ↔ folder name
  3.  MKV TITLE tag ↔ folder name
  4.  MKV TITLE tag ↔ NFO title
  5.  NFO imdbid ↔ MKV IMDB tag
  6.  Year in folder name ↔ NFO <year>
  7.  NFO <runtime> ↔ ffprobe actual duration  (catches wrong content)
  8.  Exactly one MKV in folder
  9.  MKV readable by ffprobe
  10. poster.jpg present

Outputs:
  - Markdown report  (human-readable, organized by severity)
  - JSON file        (machine-readable, for automated remediation)

Usage:
    python3 maintenance/audit_library.py
    python3 maintenance/audit_library.py /storage/media/movies
    python3 maintenance/audit_library.py /storage/media/movies --out /tmp/audit
    python3 maintenance/audit_library.py /storage/media/movies --tolerance 5
"""

import os, re, sys, json, argparse, subprocess
from pathlib import Path
from datetime import date
from xml.etree.ElementTree import parse as et_parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

DEFAULT_SCAN_DIR  = os.getenv('RC_VERIFY_SCAN_DIR', '/storage/media/movies')
FFPROBE_BIN       = os.getenv('RC_FFPROBE_BIN', 'ffprobe')
MAX_WORKERS       = 12
DEFAULT_TOLERANCE = 5   # minutes


# ── severity ──────────────────────────────────────────────────────────────────
# CRITICAL  → likely wrong video content
# WARNING   → metadata inconsistency (easily fixed)
# INFO      → missing optional files / structural notes

SEVERITY = {
    'runtime_mismatch':      'CRITICAL',
    'wrong_content_suspect': 'CRITICAL',
    'multiple_mkvs':         'CRITICAL',
    'mkv_unreadable':        'CRITICAL',
    'no_mkv':                'CRITICAL',
    'imdbid_mismatch':       'WARNING',
    'title_tag_vs_nfo':      'WARNING',
    'title_tag_vs_folder':   'WARNING',
    'nfo_title_vs_folder':   'WARNING',
    'year_mismatch':         'WARNING',
    'mkv_name_vs_folder':    'WARNING',
    'no_nfo':                'WARNING',
    'missing_poster':        'INFO',
    'no_runtime_in_nfo':     'INFO',
    'ffprobe_failed':        'INFO',
}


# ── small helpers ─────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def folder_stem(name: str) -> tuple[str, str]:
    """Split 'Some_Title_(2001)' → ('Some Title', '2001')."""
    name = re.sub(r'_?tt\d{6,9}$', '', name)
    m = re.search(r'^(.+?)_?\((\d{4})\)', name)
    if m:
        title = m.group(1).replace('_', ' ').strip()
        year  = m.group(2)
        return title, year
    return name.replace('_', ' ').strip(), ''


def mkv_stem(filename: str) -> tuple[str, str]:
    """Same split for MKV filenames (may have extra suffixes before .mkv)."""
    name = Path(filename).stem
    return folder_stem(name)


def titles_match(a: str, b: str) -> bool:
    """Fuzzy: 80 % word overlap (handles minor punctuation/article differences)."""
    wa = set(normalize(a).split())
    wb = set(normalize(b).split())
    if not wa or not wb:
        return False
    overlap = wa & wb
    return len(overlap) / max(len(wa), len(wb)) >= 0.80


# ── metadata readers ──────────────────────────────────────────────────────────

def read_nfo(nfo_path: Path) -> dict:
    out = {'title': None, 'year': None, 'runtime': None, 'imdbid': None}
    try:
        root = et_parse(nfo_path).getroot()
        for field in ('title', 'imdbid'):
            el = root.find(field)
            if el is not None and el.text:
                out[field] = el.text.strip()
        for ytag in ('year', 'releasedate'):
            el = root.find(ytag)
            if el is not None and el.text:
                m = re.match(r'(\d{4})', el.text.strip())
                if m:
                    out['year'] = m.group(1)
                    break
        el = root.find('runtime')
        if el is not None and el.text:
            try:
                out['runtime'] = int(el.text.strip())
            except ValueError:
                pass
    except Exception:
        pass
    return out


def ffprobe_info(mkv_path: Path) -> dict:
    """Returns {'duration_sec': float|None, 'readable': bool, 'title_tag': str|None, 'imdb_tag': str|None}"""
    result = {'duration_sec': None, 'readable': False, 'title_tag': None, 'imdb_tag': None}
    try:
        cmd = [
            FFPROBE_BIN, '-v', 'quiet',
            '-print_format', 'json',
            '-show_format', '-show_streams',
            str(mkv_path),
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return result
        data = json.loads(out.stdout)
        result['readable'] = True
        dur = data.get('format', {}).get('duration')
        if dur:
            result['duration_sec'] = float(dur)
        tags = data.get('format', {}).get('tags', {})
        # ffprobe lowercases tag keys
        result['title_tag'] = tags.get('title') or tags.get('TITLE')
        result['imdb_tag']  = tags.get('imdb')  or tags.get('IMDB')
    except Exception:
        pass
    return result


# ── per-folder audit ──────────────────────────────────────────────────────────

def _empty_result(folder: Path) -> dict:
    return {
        'folder':       folder.name,
        'issues':       [],   # list of {'check': str, 'severity': str, 'detail': str}
        'nfo_title':    None,
        'nfo_year':     None,
        'nfo_runtime':  None,
        'nfo_imdbid':   None,
        'actual_min':   None,
        'mkv_count':    0,
        'mkv_name':     None,
    }


def audit_folder(folder: Path, tolerance: int) -> dict:
    result = _empty_result(folder)

    def issue(check: str, detail: str):
        result['issues'].append({
            'check':    check,
            'severity': SEVERITY.get(check, 'INFO'),
            'detail':   detail,
        })

    folder_title, folder_year = folder_stem(folder.name)

    # ── Check 8: count MKVs ───────────────────────────────────────────────────
    mkvs = [f for f in folder.iterdir() if f.suffix.lower() == '.mkv' and not f.is_symlink()]
    result['mkv_count'] = len(mkvs)

    if len(mkvs) == 0:
        issue('no_mkv', 'No MKV file found in folder')
        # Can still check NFO
    elif len(mkvs) > 1:
        issue('multiple_mkvs', f'{len(mkvs)} MKV files: {[f.name for f in mkvs]}')

    mkv = mkvs[0] if mkvs else None
    result['mkv_name'] = mkv.name if mkv else None

    # ── Check 1: folder name ↔ MKV filename ──────────────────────────────────
    if mkv:
        mkv_title, mkv_year = mkv_stem(mkv.name)
        if not titles_match(folder_title, mkv_title):
            issue('mkv_name_vs_folder',
                  f'Folder title "{folder_title}" vs MKV title "{mkv_title}"')
        if folder_year and mkv_year and folder_year != mkv_year:
            issue('mkv_name_vs_folder',
                  f'Folder year {folder_year} vs MKV year {mkv_year}')

    # ── NFO ───────────────────────────────────────────────────────────────────
    nfo_path = folder / 'movie.nfo'
    if not nfo_path.exists():
        issue('no_nfo', 'movie.nfo not found')
    else:
        nfo = read_nfo(nfo_path)
        result.update({
            'nfo_title':   nfo['title'],
            'nfo_year':    nfo['year'],
            'nfo_runtime': nfo['runtime'],
            'nfo_imdbid':  nfo['imdbid'],
        })

        # ── Check 2: NFO title ↔ folder name ─────────────────────────────────
        if nfo['title'] and not titles_match(nfo['title'], folder_title):
            issue('nfo_title_vs_folder',
                  f'NFO title "{nfo["title"]}" vs folder "{folder_title}"')

        # ── Check 6: year ↔ NFO year ──────────────────────────────────────────
        if folder_year and nfo['year'] and folder_year != nfo['year']:
            issue('year_mismatch',
                  f'Folder year {folder_year} vs NFO year {nfo["year"]}')

        # ── Check 7: runtime ↔ ffprobe ────────────────────────────────────────
        if mkv:
            if nfo['runtime'] is None:
                issue('no_runtime_in_nfo', 'NFO has no <runtime> — cannot verify content')
            else:
                fp = ffprobe_info(mkv)
                if not fp['readable']:
                    issue('mkv_unreadable', 'ffprobe could not read MKV')
                elif fp['duration_sec'] is None:
                    issue('ffprobe_failed', 'ffprobe returned no duration')
                else:
                    actual_min = round(fp['duration_sec'] / 60)
                    result['actual_min'] = actual_min
                    delta = abs(actual_min - nfo['runtime'])
                    if delta > tolerance:
                        issue('runtime_mismatch',
                              f'NFO runtime {nfo["runtime"]} min vs actual {actual_min} min '
                              f'(delta {delta} min) — possible wrong content')

                # ── Checks 3 & 4: MKV TITLE tag ──────────────────────────────
                if fp.get('title_tag'):
                    tag_title = fp['title_tag']
                    if nfo['title'] and not titles_match(tag_title, nfo['title']):
                        issue('title_tag_vs_nfo',
                              f'MKV TITLE tag "{tag_title}" vs NFO title "{nfo["title"]}"')
                    if not titles_match(tag_title, folder_title):
                        issue('title_tag_vs_folder',
                              f'MKV TITLE tag "{tag_title}" vs folder "{folder_title}"')

                # ── Check 5: IMDb ID cross-check ──────────────────────────────
                if fp.get('imdb_tag') and nfo['imdbid']:
                    if fp['imdb_tag'].lower() != nfo['imdbid'].lower():
                        issue('imdbid_mismatch',
                              f'MKV IMDB tag "{fp["imdb_tag"]}" vs NFO imdbid "{nfo["imdbid"]}"')

    # ── Check 10: poster ──────────────────────────────────────────────────────
    if not (folder / 'poster.jpg').exists():
        issue('missing_poster', 'poster.jpg not found')

    return result


# ── report writers ────────────────────────────────────────────────────────────

def write_json(results: list, path: Path):
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding='utf-8')


def write_markdown(results: list, scan_dir: Path, tolerance: int, path: Path):
    today = date.today().isoformat()
    total = len(results)
    flagged = [r for r in results if r['issues']]
    clean   = total - len(flagged)

    def issues_of(sev):
        return [
            (r, i) for r in flagged
            for i in r['issues'] if i['severity'] == sev
        ]

    criticals = issues_of('CRITICAL')
    warnings  = issues_of('WARNING')
    infos     = issues_of('INFO')

    lines = []
    a = lines.append

    a(f'# Movie Library Audit — {today}')
    a(f'')
    a(f'**Library:** `{scan_dir}`  ')
    a(f'**Runtime tolerance:** ±{tolerance} min  ')
    a(f'**Total movies scanned:** {total}')
    a(f'')
    a(f'## Summary')
    a(f'')
    a(f'| | Count |')
    a(f'|---|---|')
    a(f'| ✅ Clean | {clean} |')
    a(f'| 🔴 Critical | {len(set(r["folder"] for r,_ in criticals))} movies |')
    a(f'| 🟡 Warning  | {len(set(r["folder"] for r,_ in warnings))} movies |')
    a(f'| 🔵 Info     | {len(set(r["folder"] for r,_ in infos))} movies |')
    a(f'')

    # ── issue type breakdown ──
    from collections import Counter
    check_counts = Counter(
        i['check'] for r in flagged for i in r['issues']
    )
    if check_counts:
        a(f'## Issues by Type')
        a(f'')
        a(f'| Check | Count | Severity |')
        a(f'|---|---|---|')
        for check, count in check_counts.most_common():
            sev_icon = {'CRITICAL': '🔴', 'WARNING': '🟡', 'INFO': '🔵'}.get(SEVERITY.get(check, 'INFO'), '🔵')
            a(f'| `{check}` | {count} | {sev_icon} {SEVERITY.get(check, "INFO")} |')
        a(f'')

    # ── critical movies ──
    critical_folders = [r for r in flagged if any(i['severity'] == 'CRITICAL' for i in r['issues'])]
    if critical_folders:
        a(f'## 🔴 Critical — Likely Wrong Content or Unreadable')
        a(f'')
        a(f'These need hands-on investigation before automated remediation.')
        a(f'')
        a(f'| Folder | Issue | NFO Title | NFO Runtime | Actual Runtime | IMDb ID |')
        a(f'|---|---|---|---|---|---|')
        for r in sorted(critical_folders, key=lambda x: x['folder']):
            crit_issues = [i for i in r['issues'] if i['severity'] == 'CRITICAL']
            for i in crit_issues:
                nfo_rt = f"{r['nfo_runtime']} min" if r['nfo_runtime'] else '—'
                act_rt = f"{r['actual_min']} min"  if r['actual_min']  else '—'
                a(f'| `{r["folder"]}` | {i["detail"]} | {r["nfo_title"] or "—"} | {nfo_rt} | {act_rt} | {r["nfo_imdbid"] or "—"} |')
        a(f'')

    # ── warning movies ──
    warning_folders = [r for r in flagged if any(i['severity'] == 'WARNING' for i in r['issues'])
                       and not any(i['severity'] == 'CRITICAL' for i in r['issues'])]
    if warning_folders:
        a(f'## 🟡 Warning — Metadata Inconsistencies')
        a(f'')
        a(f'Metadata disagrees internally but content is likely correct. Most can be fixed automatically.')
        a(f'')
        a(f'| Folder | Check | Detail | IMDb ID |')
        a(f'|---|---|---|---|')
        for r in sorted(warning_folders, key=lambda x: x['folder']):
            warn_issues = [i for i in r['issues'] if i['severity'] == 'WARNING']
            for i in warn_issues:
                a(f'| `{r["folder"]}` | `{i["check"]}` | {i["detail"]} | {r["nfo_imdbid"] or "—"} |')
        a(f'')

    # ── info movies ──
    info_only_folders = [r for r in flagged
                         if all(i['severity'] == 'INFO' for i in r['issues'])]
    if info_only_folders:
        a(f'## 🔵 Info — Minor / Missing Optional Files')
        a(f'')
        a(f'| Folder | Detail |')
        a(f'|---|---|')
        for r in sorted(info_only_folders, key=lambda x: x['folder']):
            for i in r['issues']:
                a(f'| `{r["folder"]}` | {i["detail"]} |')
        a(f'')

    if not flagged:
        a(f'## ✅ All movies passed all checks.')
        a(f'')

    a(f'---')
    a(f'*Generated by `maintenance/audit_library.py` on {today}*')

    path.write_text('\n'.join(lines), encoding='utf-8')


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Comprehensive movie library audit')
    parser.add_argument('scan_dir', nargs='?', default=DEFAULT_SCAN_DIR)
    parser.add_argument('--out', metavar='PATH',
                        help='Output path prefix (default: ./audit_YYYY-MM-DD). '
                             'Produces <prefix>.md and <prefix>.json')
    parser.add_argument('--tolerance', type=int, default=DEFAULT_TOLERANCE,
                        metavar='MINUTES',
                        help=f'Runtime mismatch tolerance (default {DEFAULT_TOLERANCE} min)')
    args = parser.parse_args()

    scan_dir = Path(args.scan_dir)
    if not scan_dir.is_dir():
        print(f'ERROR: not a directory: {scan_dir}')
        sys.exit(1)

    out_prefix = Path(args.out) if args.out else Path(f'audit_{date.today().isoformat()}')

    folders = sorted(f for f in scan_dir.iterdir() if f.is_dir() and not f.is_symlink())
    total   = len(folders)
    print(f'Scanning {total} movies in {scan_dir}  (runtime tolerance ±{args.tolerance} min)\n')

    results   = []
    completed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(audit_folder, f, args.tolerance): f for f in folders}
        for future in as_completed(futures):
            completed += 1
            print(f'\r  {completed}/{total}', end='', flush=True)
            try:
                results.append(future.result())
            except Exception as e:
                err = _empty_result(futures[future])
                err['issues'].append({'check': 'audit_error', 'severity': 'CRITICAL',
                                      'detail': f'{type(e).__name__}: {e}'})
                results.append(err)

    print(f'\r  {total}/{total} done\n')
    results.sort(key=lambda r: r['folder'])

    flagged = [r for r in results if r['issues']]
    critical = sum(1 for r in flagged if any(i['severity'] == 'CRITICAL' for i in r['issues']))
    warnings = sum(1 for r in flagged if any(i['severity'] == 'WARNING'  for i in r['issues'])
                                      and not any(i['severity'] == 'CRITICAL' for i in r['issues']))

    md_path   = Path(str(out_prefix) + '.md')
    json_path = Path(str(out_prefix) + '.json')

    write_markdown(results, scan_dir, args.tolerance, md_path)
    write_json(results, json_path)

    print(f'Results:')
    print(f'  ✅ Clean    : {total - len(flagged)}')
    print(f'  🔴 Critical : {critical}')
    print(f'  🟡 Warning  : {warnings}')
    print(f'  Total issues: {len(flagged)} movies flagged\n')
    print(f'  📄 {md_path}')
    print(f'  📋 {json_path}')


if __name__ == '__main__':
    main()
