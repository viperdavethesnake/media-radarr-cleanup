#!/usr/bin/env python3
"""Read-only structural and consistency audit of movie.nfo files.

Checks every movie folder in the given libraries (default: movies, movies2 and
documentaries under $RC_MEDIA_BASE) for:

- structure: missing/misnamed NFO, empty file, BOM, non-UTF-8, XML parse
  errors, wrong root, duplicate single-value tags, empty tags, literal
  "None"/"null" values, double-escaped entities (``&amp;amp;``)
- required fields: title, year, plot, runtime, premiered, genres
- value formats: year, dates, runtime, rating, votes, MPAA
- IDs: <tmdbid>/<imdbid> format and agreement with <uniqueid>, one default
  uniqueid, duplicate TMDB/IMDb IDs within or across libraries
- collections: <setid>/<collectionnumber>/uniqueid tmdbcol agreement, a
  <set> name for every collection, each collection ID mapping to one name
- names: sort title, NFO title/year vs folder name, MKV name vs folder,
  poster.jpg / fanart.jpg present

It looks only at the folder contents; it does not open the MKVs. For NFO vs
embedded MKV tags and runtime vs actual duration, use audit_library.py.

Both NFO writers are accepted as correct: batch_cleaner.py (<setid>, sort
title "Beautiful Mind, A") and regenerate_nfos.py (collectionnumber +
uniqueid type="tmdbcol"). Jellyfin reads the collection from either.

Usage:
    python3 maintenance/audit_nfos.py
    python3 maintenance/audit_nfos.py /storage/media/movies --show INFO
    python3 maintenance/audit_nfos.py --json nfo_audit.json --csv nfo_audit.csv
"""
import argparse
import csv
import json
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

load_dotenv()

MEDIA_BASE = os.getenv('RC_MEDIA_BASE', '/storage/media')
DEFAULT_SCAN_DIRS = [os.path.join(MEDIA_BASE, d) for d in ('movies', 'movies2', 'documentaries')]

SEV_ORDER = {'ERROR': 0, 'WARN': 1, 'INFO': 2}

KNOWN = {
    'title', 'originaltitle', 'sorttitle', 'year', 'premiered', 'releasedate', 'dateadded',
    'plot', 'outline', 'tagline', 'runtime', 'mpaa', 'rating', 'votes', 'tmdbid', 'imdbid',
    'uniqueid', 'genre', 'studio', 'country', 'director', 'credits', 'actor', 'set',
    'collectionnumber', 'setid', 'lockdata', 'tag', 'trailer', 'thumb', 'fanart', 'criticrating',
}
SINGLETON = {'title', 'originaltitle', 'sorttitle', 'year', 'premiered', 'releasedate',
             'dateadded', 'plot', 'tagline', 'runtime', 'mpaa', 'rating', 'votes',
             'tmdbid', 'imdbid', 'set', 'collectionnumber', 'setid'}
MPAA = {'G', 'PG', 'PG-13', 'R', 'NC-17', 'NR', 'Unrated', 'Not Rated',
        'TV-Y', 'TV-Y7', 'TV-G', 'TV-PG', 'TV-14', 'TV-MA', 'X', 'M', 'GP', 'Approved'}
FOLDER_RE = re.compile(r'^(?P<t>.+)_\((?P<y>\d{4})\)$')
ARTICLES = ('The', 'A', 'An')


def norm(s):
    """Lowercase alphanumerics only, accents folded, '&' read as 'and'."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.replace('&', 'and')
    return re.sub(r'[^a-z0-9]+', '', s.lower())


def sorttitle_ok(title, stitle):
    """True if the sort title is the title, or the title with its leading article
    dropped or moved to the end ("Beautiful Mind, A", as batch_cleaner writes)."""
    if norm(stitle) == norm(title):
        return True
    for art in ARTICLES:
        if title.startswith(art + ' '):
            rest = title[len(art) + 1:]
            if norm(stitle) in (norm(rest), norm(f'{rest}, {art}')):
                return True
    return False


def audit(folder):
    """Audit one movie folder. Returns {'folder', 'issues', ...}; never raises."""
    try:
        return _audit(folder)
    except Exception as e:  # one bad folder must not stop the scan
        return {'folder': folder, 'issues': [{'code': 'audit_crashed', 'sev': 'ERROR', 'detail': repr(e)}]}


def _audit(folder):
    issues = []

    def add(code, sev, detail=''):
        issues.append({'code': code, 'sev': sev, 'detail': detail})

    try:
        entries = os.listdir(folder)
    except OSError as e:
        return {'folder': folder, 'issues': [{'code': 'unreadable_folder', 'sev': 'ERROR', 'detail': str(e)}]}
    nfos = [e for e in entries if e.lower().endswith('.nfo')]
    mkvs = [e for e in entries if e.lower().endswith('.mkv')
            and not os.path.islink(os.path.join(folder, e))]
    info = {'folder': folder, 'issues': issues}
    if not nfos:
        add('no_nfo', 'ERROR')
        return info
    if 'movie.nfo' not in nfos:
        add('nfo_not_named_movie.nfo', 'ERROR', ','.join(nfos))
        return info
    extra = [n for n in nfos if n != 'movie.nfo']
    if extra:
        add('extra_nfo_files', 'WARN', ','.join(extra))
    path = os.path.join(folder, 'movie.nfo')
    if os.path.islink(path):
        add('nfo_is_symlink', 'WARN')
    with open(path, 'rb') as f:
        raw = f.read()
    if not raw.strip():
        add('nfo_empty', 'ERROR')
        return info
    if raw.startswith(b'\xef\xbb\xbf'):
        add('utf8_bom', 'INFO')
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError as e:
        add('not_utf8', 'ERROR', str(e))
    if not raw.lstrip(b'\xef\xbb\xbf').startswith(b'<?xml'):
        add('no_xml_declaration', 'INFO')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        add('xml_parse_error', 'ERROR', str(e))
        return info
    if root.tag != 'movie':
        add('wrong_root', 'ERROR', root.tag)
        return info

    def txt(el):
        return (el.text or '').strip() if el is not None else ''

    def g(tag):
        return txt(root.find(tag))

    # ── structure ──
    for tag, n in Counter(c.tag for c in root).items():
        if tag in SINGLETON and n > 1:
            add('duplicate_element', 'ERROR', f'{tag} x{n}')
        if tag not in KNOWN:
            add('unknown_element', 'INFO', tag)
    for c in root:
        if c.tag in ('actor', 'set', 'uniqueid'):
            continue
        t = c.text or ''
        if not t.strip() and len(c) == 0:
            add('empty_element', 'WARN', c.tag)
        elif t != t.strip():
            add('untrimmed_text', 'INFO', c.tag)
        if t.strip() in ('None', 'null', 'nan', 'NaN'):
            add('literal_none_value', 'ERROR', f'{c.tag}={t.strip()}')
        if re.search(r'&(amp|lt|gt|quot|#\d+);', t):
            add('double_escaped_entity', 'WARN', f'{c.tag}: {t.strip()[:60]}')

    title, stitle = g('title'), g('sorttitle')
    year, prem, rel = g('year'), g('premiered'), g('releasedate')
    runtime, rating, votes, mpaa = g('runtime'), g('rating'), g('votes'), g('mpaa')
    tmdbid, imdbid = g('tmdbid'), g('imdbid')
    info.update(title=title, year=year)

    # ── required fields ──
    for req in ('title', 'year', 'plot', 'runtime'):
        if not g(req):
            add(f'missing_{req}', 'ERROR' if req in ('title', 'year') else 'WARN')
    if not prem:
        add('missing_premiered', 'WARN')
    if not root.findall('genre'):
        add('no_genres', 'WARN')
    if not root.findall('actor'):
        add('no_actors', 'INFO')
    if not root.findall('director'):
        add('no_director', 'INFO')

    # ── value formats ──
    if year and not re.fullmatch(r'(18[89]\d|19\d\d|20[0-3]\d)', year):
        add('bad_year', 'ERROR', year)
    for tag, v in (('premiered', prem), ('releasedate', rel)):
        if v and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):
            add('bad_date_format', 'WARN', f'{tag}={v}')
    if year and prem and year != prem[:4]:
        add('year_vs_premiered', 'WARN', f'year={year} premiered={prem}')
    if prem and rel and prem != rel:
        add('premiered_vs_releasedate', 'INFO', f'{prem} vs {rel}')
    da = g('dateadded')
    if da and not re.fullmatch(r'\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?', da):
        add('bad_dateadded', 'WARN', da)
    if runtime:
        if not runtime.isdigit():
            add('runtime_not_int', 'WARN', runtime)
        elif int(runtime) == 0:
            add('runtime_zero', 'ERROR', runtime)
        elif int(runtime) < 40:
            add('runtime_short', 'INFO', runtime)  # shorts exist (Red Nose Day Actually = 15)
        elif int(runtime) > 300:
            add('runtime_implausible', 'WARN', runtime)
    if rating:
        try:
            if not 0 < float(rating) <= 10:
                add('rating_out_of_range', 'WARN', rating)
        except ValueError:
            add('rating_not_number', 'WARN', rating)
    if votes and not votes.isdigit():
        add('votes_not_int', 'WARN', votes)
    if mpaa and mpaa not in MPAA:
        add('nonstandard_mpaa', 'INFO', mpaa)

    # ── IDs ──
    if tmdbid and not tmdbid.isdigit():
        add('bad_tmdbid', 'ERROR', tmdbid)
    if imdbid and not re.fullmatch(r'tt\d{7,8}', imdbid):
        add('bad_imdbid', 'ERROR', imdbid)
    uids = root.findall('uniqueid')
    by_type = defaultdict(list)
    for u in uids:
        t = u.get('type')
        if not t:
            add('uniqueid_no_type', 'ERROR', txt(u))
        if not txt(u):
            add('uniqueid_empty', 'ERROR', t or '')
        by_type[t].append(txt(u))
    for t, vals in by_type.items():
        if len(vals) > 1:
            add('duplicate_uniqueid_type', 'ERROR', f'{t}: {vals}')
    defaults = [u.get('type') for u in uids if u.get('default') == 'true']
    if len(defaults) > 1:
        add('multiple_default_uniqueid', 'ERROR', ','.join(map(str, defaults)))
    elif uids and defaults != ['tmdb']:
        add('default_uniqueid_not_tmdb', 'WARN', ','.join(map(str, defaults)) or 'none')
    u_tmdb = (by_type.get('tmdb') or [''])[0]
    u_imdb = (by_type.get('imdb') or [''])[0]
    if u_imdb and not re.fullmatch(r'tt\d{7,8}', u_imdb):
        add('bad_imdbid', 'ERROR', f'uniqueid={u_imdb}')
    if u_tmdb and not u_tmdb.isdigit():
        add('bad_tmdbid', 'ERROR', f'uniqueid={u_tmdb}')
    if not (tmdbid or u_tmdb):
        add('no_tmdb_id', 'ERROR')
    if not (imdbid or u_imdb):
        add('no_imdb_id', 'WARN')
    if tmdbid and u_tmdb and tmdbid != u_tmdb:
        add('tmdbid_vs_uniqueid', 'ERROR', f'{tmdbid} vs {u_tmdb}')
    if imdbid and u_imdb and imdbid != u_imdb:
        add('imdbid_vs_uniqueid', 'ERROR', f'{imdbid} vs {u_imdb}')
    if (tmdbid or imdbid) and not uids:
        add('no_uniqueid', 'WARN')
    info['tmdbid'] = tmdbid or u_tmdb
    info['imdbid'] = imdbid or u_imdb

    # ── collections ──
    sets = root.findall('set')
    colnum = g('collectionnumber')
    u_col = (by_type.get('tmdbcol') or [''])[0]
    setid = g('setid')
    ids = {k: v for k, v in (('setid', setid), ('collectionnumber', colnum), ('tmdbcol', u_col)) if v}
    if len(set(ids.values())) > 1:
        add('collection_ids_disagree', 'ERROR', ' '.join(f'{k}={v}' for k, v in ids.items()))
    for k, v in ids.items():
        if not v.isdigit():
            add('bad_collection_id', 'ERROR', f'{k}={v}')
    if ids and not sets:
        add('collection_id_without_set', 'WARN', ','.join(ids.values()))
    for s in sets:
        if not txt(s.find('name')) and not txt(s):
            add('set_without_name', 'ERROR')
    if sets and not ids:
        add('set_without_collection_id', 'INFO')
    info['set'] = (txt(sets[0].find('name')) or txt(sets[0])) if sets else ''
    info['colid'] = setid or colnum or u_col

    # ── actors and repeated list values ──
    orders = []
    for a in root.findall('actor'):
        if not txt(a.find('name')):
            add('actor_without_name', 'WARN')
        o = txt(a.find('order'))
        if o:
            orders.append(o)
    if orders and orders != [str(i) for i in range(len(orders))]:
        add('actor_order_nonsequential', 'INFO', ','.join(orders[:10]))
    for tag in ('genre', 'studio', 'country', 'director'):
        dup = [v for v, n in Counter(txt(e) for e in root.findall(tag)).items() if n > 1]
        if dup:
            # TMDB itself sometimes lists a studio twice; copied verbatim, cosmetic
            add(f'duplicate_{tag}', 'INFO' if tag == 'studio' else 'WARN', ','.join(dup))
    names = [txt(a.find('name')) for a in root.findall('actor')]
    dupa = [v for v, n in Counter(names).items() if n > 1 and v]
    if dupa:
        add('duplicate_actor', 'INFO', ','.join(dupa))

    # ── titles ──
    if title and stitle and not sorttitle_ok(title, stitle):
        add('sorttitle_unrelated', 'WARN', f'{title!r} / {stitle!r}')
    if title and re.search(r'\(\d{4}\)\s*$', title):
        add('year_in_title', 'WARN', title)
    if '_' in title:
        add('underscore_in_title', 'WARN', title)

    # ── folder / file names ──
    base = os.path.basename(folder)
    m = FOLDER_RE.match(base)
    if not m:
        add('folder_name_nonstandard', 'WARN', base)
    else:
        ft, fy = m.group('t').replace('_', ' '), m.group('y')
        if year and fy != year:
            add('folder_year_vs_nfo', 'WARN', f'folder={fy} nfo={year}')
        if title and norm(ft) != norm(title):
            add('folder_title_vs_nfo', 'WARN', f'folder={ft!r} nfo={title!r}')
    if not mkvs:
        add('no_mkv_in_folder', 'ERROR')
    for mk in mkvs:
        stem = re.sub(r'_\[[^\]]*\]$', '', os.path.splitext(mk)[0])
        if stem != base:
            add('mkv_name_vs_folder', 'WARN', mk)
    for art in ('poster.jpg', 'fanart.jpg'):
        if art not in entries:
            add(f'missing_{art}', 'INFO')
    return info


def cross_check(results):
    """Add issues that need the whole scan: duplicate IDs and collection names."""
    for key in ('tmdbid', 'imdbid'):
        seen = defaultdict(list)
        for r in results:
            if r.get(key):
                seen[r[key]].append(r)
        for v, rs in seen.items():
            if len(rs) < 2:
                continue
            for r in rs:
                others = [o for o in rs if o is not r]
                same_lib = any(os.path.dirname(o['folder']) == os.path.dirname(r['folder']) for o in others)
                r['issues'].append({
                    'code': f'duplicate_{key}_{"same_lib" if same_lib else "cross_lib"}',
                    'sev': 'ERROR' if same_lib else 'WARN',
                    'detail': f'{v} also in {[o["folder"] for o in others]}',
                })
    names = defaultdict(set)
    for r in results:
        if r.get('colid'):
            names[r['colid']].add(r.get('set', ''))
    for r in results:
        if r.get('colid') and len(names[r['colid']]) > 1:
            r['issues'].append({'code': 'collection_name_inconsistent', 'sev': 'WARN',
                                'detail': f"{r['colid']}: {sorted(names[r['colid']])}"})


def list_folders(libs, errors):
    folders = []
    for lib in libs:
        try:
            names = sorted(os.listdir(lib))
        except OSError as e:
            errors.append(f'{lib}: {e}')
            continue
        for e in names:
            p = os.path.join(lib, e)
            if not e.startswith('.') and os.path.isdir(p) and not os.path.islink(p):
                folders.append(p)
    return folders


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('paths', nargs='*', help=f'library roots (default: {", ".join(DEFAULT_SCAN_DIRS)})')
    ap.add_argument('--show', choices=list(SEV_ORDER), default='WARN',
                    help='list individual issues at this severity or worse (default WARN)')
    ap.add_argument('--limit', type=int, help='audit only the first N folders')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--json', metavar='FILE', help='write every folder and its issues as JSON')
    ap.add_argument('--csv', metavar='FILE', help='write one row per issue as CSV')
    args = ap.parse_args()

    errors = []
    folders = list_folders(args.paths or DEFAULT_SCAN_DIRS, errors)
    if args.limit:
        folders = folders[:args.limit]
    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(audit, folders))
    cross_check(results)

    rows = sorted(((SEV_ORDER[i['sev']], r['folder'], i) for r in results for i in r['issues']),
                  key=lambda x: (x[0], x[2]['code'], x[1]))
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(results, f, indent=1, ensure_ascii=False)
    if args.csv:
        with open(args.csv, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['severity', 'code', 'folder', 'detail'])
            for _, folder, i in rows:
                w.writerow([i['sev'], i['code'], folder, i['detail']])

    tally = Counter((i['sev'], i['code']) for _, _, i in rows)
    print(f'{len(results)} folders scanned; {sum(1 for r in results if r["issues"])} with issues')
    for (sev, code), n in sorted(tally.items(), key=lambda x: (SEV_ORDER[x[0][0]], -x[1], x[0][1])):
        print(f'  {sev:5} {n:5}  {code}')
    shown = [x for x in rows if x[0] <= SEV_ORDER[args.show]]
    if shown:
        print(f'\nIssues at {args.show} or worse:')
        for _, folder, i in shown:
            print(f'  {i["sev"]:5} {i["code"]:28} {folder}' + (f'  ({i["detail"]})' if i['detail'] else ''))
    for e in errors:
        print(f'[ERROR] {e}', file=sys.stderr)
    if errors:
        sys.exit(1)


if __name__ == '__main__':
    main()
