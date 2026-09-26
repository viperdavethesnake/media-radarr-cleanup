#!/usr/bin/env python3
"""
Read-only inspector for foreign-original films.

The movie pipeline refuses these (batch_cleaner routes original_language != 'en'
to ./failed/), and its track logic is actively wrong for them: it retags whatever
audio it picks as 'eng' and drops forced subtitles. Foreign films are handled
per-film instead, and this script produces the report we decide from.

It never modifies a file. Output: console table + optional --json / --md.
"""

import os, sys, json, time, argparse, subprocess, signal, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'movie_cleanup'))

from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env'))

from batch_cleaner import find_imdbid, fetch_tmdb_metadata  # noqa: E402

SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
FOREIGN_DIR = os.getenv('RC_FOREIGN_DIR') or os.path.join(SERVARR_DIR, 'foreign')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
MAX_WORKERS = 4

# TMDB reports ISO 639-1; Matroska tracks carry 639-2/B. Match on both.
ISO_1_TO_2 = {
    'it': 'ita', 'de': 'ger', 'fr': 'fre', 'es': 'spa', 'ja': 'jpn',
    'ko': 'kor', 'zh': 'chi', 'pt': 'por', 'ru': 'rus', 'nl': 'dut',
    'sv': 'swe', 'no': 'nor', 'da': 'dan', 'fi': 'fin', 'pl': 'pol',
    'tr': 'tur', 'ar': 'ara', 'hi': 'hin', 'th': 'tha', 'el': 'gre',
    'he': 'heb', 'cs': 'cze', 'hu': 'hun', 'ro': 'rum', 'uk': 'ukr',
    'vi': 'vie', 'id': 'ind', 'ms': 'may', 'en': 'eng',
    # TMDB uses a non-standard 'cn' for Cantonese (the Ip Man films); real
    # sources tag those tracks 'chi'/'zho'/'yue'. Without this the whole
    # Chinese-language library reports ORIG_AUDIO_MISSING.
    'cn': 'chi',
}

# Languages that should be treated as the same language for track matching.
# Each set is closed over both ISO 639-1 and 639-2/B spellings.
LANG_ALIASES = [
    {'zh', 'cn', 'chi', 'zho', 'yue', 'cmn'},   # Mandarin / Cantonese / generic Chinese
    {'he', 'heb', 'iw'},                        # Hebrew (legacy 'iw' still appears)
    {'id', 'ind', 'in'},                        # Indonesian (legacy 'in')
    {'no', 'nor', 'nb', 'nob', 'nn', 'nno'},    # Norwegian Bokmål / Nynorsk
    {'de', 'ger', 'deu'}, {'fr', 'fre', 'fra'}, {'es', 'spa'},
    {'cs', 'cze', 'ces'}, {'el', 'gre', 'ell'}, {'nl', 'dut', 'nld'},
    {'ro', 'rum', 'ron'}, {'ms', 'may', 'msa'}, {'pt', 'por'},
]

TEXT_SUB_CODECS = ('S_TEXT/UTF8', 'S_TEXT/ASS', 'S_TEXT/SSA')
IMAGE_SUB_CODECS = ('S_HDMV/PGS', 'S_VOBSUB', 'S_DVBSUB')

shutdown_requested = False


def signal_handler(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    log("⚠️ Interrupt received, finishing in-flight probes...")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def log(msg):
    line = f"{time.strftime('[%Y-%m-%d %H:%M:%S]')} {msg}"
    print(line)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, 'inspect_foreign.log'), 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def mkv_identify(mkv_path):
    # 300s like batch_cleaner's strip_attachments: this is only a metadata read,
    # but it queues behind other big I/O on the same storage-bound ZFS pool.
    result = subprocess.run(['mkvmerge', '-J', mkv_path],
                            capture_output=True, text=True, check=True, timeout=300)
    return json.loads(result.stdout)


def lang_matches(track_lang, ietf, iso1):
    """True if a track's language is `iso1` (given as ISO 639-1)."""
    if not iso1:
        return False
    want = {iso1.lower(), ISO_1_TO_2.get(iso1.lower(), '')} - {''}
    for alias in LANG_ALIASES:
        if want & alias:
            want |= alias
    got = {(track_lang or '').lower(), (ietf or '').lower().split('-')[0]}
    return bool(want & got)


def sub_kind(codec_id):
    if codec_id in TEXT_SUB_CODECS:
        return 'text'
    if codec_id in IMAGE_SUB_CODECS:
        return 'image'
    return 'other'


def sub_flavor(track):
    """Classify a subtitle track: full / forced / sdh / dubtitle / commentary."""
    p = track.get('properties') or {}
    name = (p.get('track_name') or '').lower()
    if 'commentary' in name or p.get('flag_commentary'):
        return 'commentary'
    if 'dubtitle' in name:
        return 'dubtitle'
    if 'sdh' in name or 'hearing' in name or 'impaired' in name or p.get('flag_hearing_impaired'):
        return 'sdh'
    if 'forced' in name or p.get('forced_track'):
        return 'forced'
    return 'full'


def describe_track(t):
    p = t.get('properties') or {}
    return {
        'id': t.get('id'),
        'type': t.get('type'),
        'codec_id': p.get('codec_id'),
        'language': p.get('language'),
        'language_ietf': p.get('language_ietf'),
        'track_name': p.get('track_name'),
        'default': bool(p.get('default_track')),
        'forced': bool(p.get('forced_track')),
        'channels': p.get('audio_channels'),
        'dimensions': p.get('pixel_dimensions'),
        'flavor': sub_flavor(t) if t.get('type') == 'subtitles' else None,
        'kind': sub_kind(p.get('codec_id')) if t.get('type') == 'subtitles' else None,
    }


def analyze(film):
    """Observations only. States what is in the file; decisions are David's."""
    findings = []
    orig = film.get('original_language')
    tracks = film['tracks']

    audio = [t for t in tracks if t['type'] == 'audio']
    subs = [t for t in tracks if t['type'] == 'subtitles']
    video = [t for t in tracks if t['type'] == 'video']

    def obs(code, msg):
        findings.append({'code': code, 'message': msg})

    # --- audio ---
    orig_audio = [t for t in audio if lang_matches(t['language'], t['language_ietf'], orig)]
    eng_audio = [t for t in audio if lang_matches(t['language'], t['language_ietf'], 'en')]

    if not orig_audio:
        obs('ORIG_AUDIO_ABSENT',
            f"No audio track tagged as the original language ({orig})")
    if not any(t['default'] for t in audio):
        obs('NO_DEFAULT_AUDIO', "No audio track carries the default flag")
    n_default_audio = sum(1 for t in audio if t['default'])
    if n_default_audio > 1:
        obs('MULTI_DEFAULT_AUDIO', f"{n_default_audio} audio tracks carry the default flag")
    if orig_audio and not any(t['default'] for t in orig_audio):
        obs('ORIG_AUDIO_NOT_DEFAULT',
            f"Original-language ({orig}) audio is not the default track")
    for t in audio:
        if t['forced']:
            obs('AUDIO_FORCED_FLAG',
                f"Audio track {t['id']} ({t['language']}) carries the forced flag")

    # --- subtitles ---
    eng_subs = [t for t in subs if lang_matches(t['language'], t['language_ietf'], 'en')]
    eng_full = [t for t in eng_subs if t['flavor'] == 'full']
    eng_full_text = [t for t in eng_full if t['kind'] == 'text']
    eng_full_image = [t for t in eng_full if t['kind'] == 'image']
    eng_forced = [t for t in eng_subs if t['flavor'] == 'forced']

    if not eng_subs:
        obs('ENG_SUB_NONE', "No English subtitle tracks")
    elif not eng_full:
        obs('ENG_SUB_NO_FULL',
            f"English subs present, none full "
            f"({', '.join(sorted({t['flavor'] for t in eng_subs}))} only)")
    else:
        if eng_full_text:
            obs('ENG_SUB_FULL_TEXT', f"{len(eng_full_text)} full text English sub track(s)")
        if eng_full_image:
            obs('ENG_SUB_FULL_IMAGE',
                f"{len(eng_full_image)} full image-based English sub track(s) "
                f"({eng_full_image[0]['codec_id']})")

    if eng_full and not any(t['default'] for t in eng_full):
        obs('ENG_SUB_NOT_DEFAULT', "No full English sub carries the default flag")
    if eng_forced:
        obs('ENG_SUB_FORCED_PRESENT',
            f"Forced English sub track(s): ids {[t['id'] for t in eng_forced]}")

    other_subs = [t for t in subs if t not in eng_subs]
    if other_subs:
        langs = sorted({t['language'] or 'und' for t in other_subs})
        obs('OTHER_SUBS',
            f"{len(other_subs)} non-English sub tracks ({', '.join(langs)})")

    for t in video:
        if t['language'] and t['language'] not in ('und',):
            obs('VIDEO_LANG_TAGGED', f"Video track language tag is {t['language']!r}")

    film['findings'] = findings
    film['summary'] = {
        'audio_total': len(audio),
        'audio_original': len(orig_audio),
        'audio_english': len(eng_audio),
        'subs_total': len(subs),
        'subs_english_full_text': len(eng_full_text),
        'subs_english_full_image': len(eng_full_image),
    }
    return film


def inspect_folder(folder):
    base = os.path.basename(folder)
    film = {'folder': base, 'path': folder, 'tracks': [], 'findings': []}
    try:
        mkvs = [f for f in os.listdir(folder) if f.lower().endswith('.mkv')]
        if not mkvs:
            film['error'] = 'No MKV in folder'
            return film
        if len(mkvs) > 1:
            film['error'] = f'{len(mkvs)} MKVs in folder — resolve manually'
            return film

        mkv = mkvs[0]
        path = os.path.join(folder, mkv)
        film['mkv'] = mkv
        film['size_gb'] = round(os.path.getsize(path) / 1024**3, 1)

        # Sidecars the movie pipeline would have produced.
        film['sidecars'] = {n: os.path.isfile(os.path.join(folder, n))
                            for n in ('movie.nfo', 'poster.jpg', 'fanart.jpg')}

        imdbid = find_imdbid(folder, mkv)
        film['imdb'] = imdbid
        if imdbid:
            try:
                meta, _, _ = fetch_tmdb_metadata(imdbid)
                film['tmdb'] = meta.get('id')
                film['title'] = meta.get('title')
                film['original_title'] = meta.get('original_title')
                film['year'] = (meta.get('release_date') or '')[:4]
                film['original_language'] = meta.get('original_language')
                film['spoken_languages'] = [s.get('iso_639_1') for s in meta.get('spoken_languages', [])]
            except Exception as e:
                film['tmdb_error'] = str(e)
        else:
            film['tmdb_error'] = 'No IMDb ID found in filename or movie.nfo'

        info = mkv_identify(path)
        film['tracks'] = [describe_track(t) for t in info.get('tracks') or []]
        film['attachments'] = len(info.get('attachments') or [])
        film['chapters'] = bool(info.get('chapters'))

        return analyze(film)

    except Exception as e:
        film['error'] = f'{e}'
        log(f"❌ {base}: {e}\n{traceback.format_exc()}")
        return film


def print_film(film):
    print()
    print('=' * 78)
    title = film.get('title') or film['folder']
    header = f"{title} ({film.get('year', '?')})"
    if film.get('original_language'):
        header += f"  [original: {film['original_language']}]"
    print(header)
    print(f"  {film.get('mkv', '—')}  ({film.get('size_gb', '?')} GB)")
    if film.get('error'):
        print(f"  ❌ {film['error']}")
        return
    if film.get('tmdb_error'):
        print(f"  ⚠️  TMDB: {film['tmdb_error']}")

    print('-' * 78)
    for t in film['tracks']:
        flags = []
        if t['default']:
            flags.append('default')
        if t['forced']:
            flags.append('forced')
        extra = ''
        if t['type'] == 'subtitles':
            extra = f"{t['kind']}/{t['flavor']}"
        elif t['type'] == 'audio' and t['channels']:
            extra = f"{t['channels']}ch"
        elif t['type'] == 'video' and t['dimensions']:
            extra = t['dimensions']
        print(f"  {t['id']:>2}  {t['type']:<9} {(t['language'] or '—'):<5} "
              f"{(t['codec_id'] or '—'):<16} {extra:<14} "
              f"{','.join(flags):<14} {t['track_name'] or ''}")

    print('-' * 78)
    for f in film['findings']:
        print(f"  - [{f['code']}] {f['message']}")


def to_markdown(films):
    lines = ['# Foreign film inspection report', '',
             f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} from `{FOREIGN_DIR}`", '',
             '| Film | Orig | Size | Audio (orig/eng/total) | Eng full subs | Notes |',
             '|---|---|---|---|---|---|']
    for f in films:
        if f.get('error'):
            lines.append(f"| {f['folder']} | — | — | — | — | {f['error']} |")
            continue
        s = f['summary']
        subs = []
        if s['subs_english_full_text']:
            subs.append(f"{s['subs_english_full_text']} text")
        if s['subs_english_full_image']:
            subs.append(f"{s['subs_english_full_image']} image")
        lines.append(
            f"| {f.get('title') or f['folder']} ({f.get('year', '?')}) "
            f"| {f.get('original_language', '?')} | {f.get('size_gb')} GB "
            f"| {s['audio_original']}/{s['audio_english']}/{s['audio_total']} "
            f"| {', '.join(subs) or 'none'} "
            f"| {len(f['findings'])} |")

    for f in films:
        if f.get('error'):
            continue
        lines += ['', f"## {f.get('title') or f['folder']} ({f.get('year', '?')})", '',
                  f"- File: `{f.get('mkv')}` ({f.get('size_gb')} GB)",
                  f"- IMDb: `{f.get('imdb')}` · TMDB: `{f.get('tmdb')}` · "
                  f"original language: `{f.get('original_language')}`", '',
                  '| # | Type | Lang | Codec | Flavor | Flags | Name |', '|---|---|---|---|---|---|---|']
        for t in f['tracks']:
            flags = ','.join([x for x, on in (('default', t['default']), ('forced', t['forced'])) if on])
            flavor = f"{t['kind']}/{t['flavor']}" if t['type'] == 'subtitles' else (
                f"{t['channels']}ch" if t['channels'] else (t['dimensions'] or ''))
            lines.append(f"| {t['id']} | {t['type']} | {t['language'] or '—'} | "
                         f"`{t['codec_id'] or '—'}` | {flavor} | {flags} | {t['track_name'] or ''} |")
        lines.append('')
        for fd in f['findings']:
            lines.append(f"- **{fd['code']}** — {fd['message']}")
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dir', default=FOREIGN_DIR, help=f'Directory to scan (default: {FOREIGN_DIR})')
    ap.add_argument('--film', action='append', help='Inspect only this folder name (repeatable)')
    ap.add_argument('--limit', type=int, help='Stop after N films')
    ap.add_argument('--json', help='Write full report as JSON')
    ap.add_argument('--md', help='Write report as Markdown')
    ap.add_argument('--workers', type=int, default=MAX_WORKERS)
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        log(f"❌ Not a directory: {args.dir}")
        return 1

    folders = sorted(os.path.join(args.dir, d) for d in os.listdir(args.dir)
                     if os.path.isdir(os.path.join(args.dir, d)) and not d.startswith('.'))
    if args.film:
        wanted = set(args.film)
        folders = [f for f in folders if os.path.basename(f) in wanted]
    if args.limit:
        folders = folders[:args.limit]

    log(f"▶ Inspecting {len(folders)} film(s) in {args.dir} (read-only, {args.workers} workers)")

    films = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(inspect_folder, f): f for f in folders}
        for fut in as_completed(futures):
            if shutdown_requested:
                break
            try:
                films.append(fut.result())
            except Exception as e:
                log(f"❌ Worker error on {futures[fut]}: {e}")

    films.sort(key=lambda f: f['folder'])
    for f in films:
        print_film(f)

    errored = [f for f in films if f.get('error')]
    print()
    print('=' * 78)
    print(f"{len(films)} film(s) inspected"
          + (f" · {len(errored)} could not be read" if errored else ""))
    print('=' * 78)

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(films, fh, indent=2, ensure_ascii=False)
        log(f"📄 JSON written: {args.json}")
    if args.md:
        with open(args.md, 'w', encoding='utf-8') as fh:
            fh.write(to_markdown(films))
        log(f"📄 Markdown written: {args.md}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
