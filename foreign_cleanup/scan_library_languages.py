#!/usr/bin/env python3
"""
Find foreign-original films sitting in an English-pipeline library.

Read-only. Parses each folder's movie.nfo for its TMDB id, asks TMDB for
original_language, and reports everything that is not English. Results are
cached to JSON so the network pass only runs once.

Nothing is moved or modified.
"""

import os, sys, json, re, time, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env'))

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')

_print_lock = threading.Lock()


def log(msg):
    line = f"{time.strftime('[%Y-%m-%d %H:%M:%S]')} {msg}"
    with _print_lock:
        print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, 'scan_library_languages.log'), 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def parse_nfo(path):
    """Pull ids and titles out of a movie.nfo. Regex fallback for malformed XML."""
    out = {}
    try:
        root = ET.parse(path).getroot()
        for tag in ('title', 'originaltitle', 'year', 'tmdbid', 'imdbid'):
            el = root.find(tag)
            if el is not None and el.text:
                out[tag] = el.text.strip()
        for uid in root.findall('uniqueid'):
            if uid.get('type') == 'tmdb' and uid.text and 'tmdbid' not in out:
                out['tmdbid'] = uid.text.strip()
            if uid.get('type') == 'imdb' and uid.text and 'imdbid' not in out:
                out['imdbid'] = uid.text.strip()
        out['countries'] = [c.text for c in root.findall('country') if c.text]
    except Exception:
        try:
            txt = open(path, encoding='utf-8', errors='ignore').read()
            m = re.search(r'<tmdbid>(\d+)</tmdbid>', txt)
            if m:
                out['tmdbid'] = m.group(1)
            m = re.search(r'(tt\d{6,9})', txt)
            if m:
                out['imdbid'] = m.group(1)
            m = re.search(r'<title>(.*?)</title>', txt, re.S)
            if m:
                out['title'] = m.group(1).strip()
        except Exception:
            pass
    return out


def tmdb_language(tmdb_id, session):
    r = session.get(f'https://api.themoviedb.org/3/movie/{tmdb_id}',
                    params={'api_key': TMDB_API_KEY}, timeout=30)
    if r.status_code == 429:
        time.sleep(float(r.headers.get('Retry-After', 2)))
        return tmdb_language(tmdb_id, session)
    r.raise_for_status()
    d = r.json()
    return {
        'original_language': d.get('original_language'),
        'original_title': d.get('original_title'),
        'tmdb_title': d.get('title'),
        'spoken_languages': [s.get('iso_639_1') for s in d.get('spoken_languages') or []],
        'runtime': d.get('runtime'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default='/storage/media/movies')
    ap.add_argument('--cache', default=os.path.join(LOG_DIR, 'library_languages.json'))
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--limit', type=int)
    ap.add_argument('--json', help='Write the foreign-film report here')
    args = ap.parse_args()

    if not TMDB_API_KEY:
        log('❌ TMDB_API_KEY not set')
        return 1

    cache = {}
    if os.path.isfile(args.cache):
        try:
            cache = json.load(open(args.cache))
            log(f'📁 cache: {len(cache)} films already resolved')
        except Exception:
            cache = {}

    folders = sorted(d for d in os.listdir(args.dir)
                     if os.path.isdir(os.path.join(args.dir, d)) and not d.startswith('.'))
    if args.limit:
        folders = folders[:args.limit]

    films, no_id = [], []
    for d in folders:
        nfo = os.path.join(args.dir, d, 'movie.nfo')
        if not os.path.isfile(nfo):
            no_id.append({'folder': d, 'reason': 'no movie.nfo'})
            continue
        meta = parse_nfo(nfo)
        if not meta.get('tmdbid'):
            no_id.append({'folder': d, 'reason': 'no tmdbid in nfo'})
            continue
        meta['folder'] = d
        films.append(meta)

    todo = [f for f in films if f['tmdbid'] not in cache]
    log(f'▶ {len(films)} films with ids · {len(todo)} need lookup · '
        f'{len(no_id)} unidentifiable · {args.workers} workers')

    if todo:
        session = requests.Session()
        done = 0

        def work(f):
            try:
                return f['tmdbid'], tmdb_language(f['tmdbid'], session)
            except Exception as e:
                return f['tmdbid'], {'error': str(e)}

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(work, f) for f in todo]
            for fut in as_completed(futures):
                tid, res = fut.result()
                cache[tid] = res
                done += 1
                if done % 100 == 0:
                    log(f'  … {done}/{len(todo)}')
                    json.dump(cache, open(args.cache, 'w'), indent=0)

        json.dump(cache, open(args.cache, 'w'), indent=0)
        log(f'💾 cache written: {args.cache}')

    foreign, errors = [], []
    for f in films:
        c = cache.get(f['tmdbid']) or {}
        if c.get('error'):
            errors.append({**f, 'error': c['error']})
            continue
        lang = c.get('original_language')
        if lang and lang != 'en':
            foreign.append({
                'folder': f['folder'],
                'title': f.get('title') or c.get('tmdb_title'),
                'original_title': c.get('original_title'),
                'year': f.get('year'),
                'lang': lang,
                'spoken': c.get('spoken_languages') or [],
                'countries': f.get('countries') or [],
                'tmdbid': f['tmdbid'],
                'imdbid': f.get('imdbid'),
                'title_differs': (c.get('original_title') or '') != (c.get('tmdb_title') or ''),
            })

    foreign.sort(key=lambda x: (x['lang'], x['folder']))

    print()
    print('=' * 78)
    print(f'{len(foreign)} foreign-original films in {args.dir}')
    print('=' * 78)
    from collections import Counter
    for lang, n in Counter(f['lang'] for f in foreign).most_common():
        print(f'  {lang}: {n}')
    print()
    for f in foreign:
        star = '*' if f['title_differs'] else ' '
        print(f"{star} [{f['lang']}] {f['title']} ({f['year']})")
        if f['title_differs']:
            print(f"      original: {f['original_title']}")
    if errors:
        print(f'\n⚠️ {len(errors)} lookup errors')
    if no_id:
        print(f'⚠️ {len(no_id)} folders without usable ids')

    if args.json:
        json.dump({'foreign': foreign, 'errors': errors, 'no_id': no_id},
                  open(args.json, 'w'), indent=2, ensure_ascii=False)
        log(f'📄 {args.json}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
