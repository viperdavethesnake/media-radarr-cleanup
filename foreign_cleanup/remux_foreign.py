#!/usr/bin/env python3
"""
Remux foreign-original films to an explicit per-film track spec.

Unlike the English pipeline this makes no track *choices* — the spec says
exactly which tracks survive, and this validates the file still matches that
spec before touching anything. If a track's language or codec has moved, it
aborts that film rather than guessing.

Dry-run by default (repo convention); --run to execute.
"""

import os, sys, json, time, shutil, signal, argparse, subprocess, threading, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'movie_cleanup'))

from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env'))

from batch_cleaner import (fetch_tmdb_metadata, write_tags_xml, set_tags_in_mkv,  # noqa: E402
                           write_nfo, download_image)
from mkv_remux_cleanroom import enhanced_file_name  # noqa: E402

SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
CLEANED_DIR = os.path.join(SERVARR_DIR, 'cleaned')
REMUX_TIMEOUT = 3600      # per repo standard; ~2.7x headroom over projection here

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


def log(msg):
    line = f"{time.strftime('[%Y-%m-%d %H:%M:%S]')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, 'remux_foreign.log'), 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def mkv_identify(path):
    r = subprocess.run(['mkvmerge', '-J', path],
                       capture_output=True, text=True, check=True, timeout=300)
    return json.loads(r.stdout)


def validate(spec, info):
    """Confirm the file still matches the spec. Returns list of problems."""
    problems = []
    obsolete = sorted({'subtitle_srt', 'sub_lang', 'sub_name'} & set(spec))
    if obsolete:
        problems.append(f"obsolete spec keys {obsolete}: use the 'subtitles' list instead")
    tracks = {t['id']: t for t in info.get('tracks') or []}

    def check(tid, want_type, want_lang=None, want_codec=None, label=''):
        t = tracks.get(tid)
        if not t:
            problems.append(f"{label}: track {tid} missing")
            return
        if t['type'] != want_type:
            problems.append(f"{label}: track {tid} is {t['type']}, expected {want_type}")
        p = t.get('properties') or {}
        if want_lang and p.get('language') != want_lang:
            problems.append(f"{label}: track {tid} language is {p.get('language')}, expected {want_lang}")
        if want_codec and want_codec not in (p.get('codec_id') or ''):
            problems.append(f"{label}: track {tid} codec is {p.get('codec_id')}, expected {want_codec}")

    check(spec['video'], 'video', label='video')
    check(spec['audio_default'], 'audio',
          spec.get('audio_default_lang'), spec.get('audio_default_codec'), 'audio_default')
    if spec.get('audio_secondary') is not None:
        check(spec['audio_secondary'], 'audio',
              spec.get('audio_secondary_lang'), spec.get('audio_secondary_codec'), 'audio_secondary')

    for s in spec.get('subtitles') or []:
        if s.get('file') and not os.path.isfile(s['file']):
            problems.append(f"subtitle file not found: {s['file']}")
        if s.get('source_track') is not None:
            t = tracks.get(s['source_track'])
            if not t:
                problems.append(f"subtitle: source track {s['source_track']} missing")
            elif t['type'] != 'subtitles':
                problems.append(f"subtitle: track {s['source_track']} is {t['type']}, not subtitles")
            elif s.get('expect_name') and (t.get('properties') or {}).get('track_name') != s['expect_name']:
                problems.append(f"subtitle: track {s['source_track']} is named "
                                f"{(t.get('properties') or {}).get('track_name')!r}, "
                                f"expected {s['expect_name']!r}")
    return problems


def build_cmd(spec, src, dst):
    """
    Subtitles come from either place, and a film can mix both:
      {"source_track": 9, ...}  — a track already inside the source MKV
      {"file": "/path.srt", ...} — an external file appended as a new input
    Each carries its own lang / name / default / forced.
    """
    audio_ids = [str(spec['audio_default'])]
    if spec.get('audio_secondary') is not None:
        audio_ids.append(str(spec['audio_secondary']))

    subs = spec.get('subtitles') or []
    internal = [s for s in subs if s.get('source_track') is not None]
    external = [s for s in subs if s.get('file')]

    cmd = ['mkvmerge', '-o', dst,
           '--video-tracks', str(spec['video']),
           '--audio-tracks', ','.join(audio_ids)]

    if internal:
        cmd += ['--subtitle-tracks', ','.join(str(s['source_track']) for s in internal)]
    else:
        cmd.append('--no-subtitles')   # drops every PGS in the source

    cmd.append('--no-attachments')
    cmd += ['--title', '']   # mkvmerge otherwise carries the source segment title through

    if not spec.get('keep_chapters', True):
        cmd.append('--no-chapters')

    cmd += ['--language', f"{spec['audio_default']}:{spec.get('audio_default_lang', 'und')}",
            '--default-track-flag', f"{spec['audio_default']}:1",
            '--forced-display-flag', f"{spec['audio_default']}:0"]
    if spec.get('audio_default_name') is not None:
        cmd += ['--track-name', f"{spec['audio_default']}:{spec['audio_default_name']}"]
    if spec.get('audio_secondary') is not None:
        cmd += ['--language', f"{spec['audio_secondary']}:{spec.get('audio_secondary_lang', 'und')}",
                '--default-track-flag', f"{spec['audio_secondary']}:0",
                '--forced-display-flag', f"{spec['audio_secondary']}:0"]
        if spec.get('audio_secondary_name') is not None:
            cmd += ['--track-name', f"{spec['audio_secondary']}:{spec['audio_secondary_name']}"]

    for s in internal:
        tid = s['source_track']
        cmd += ['--language', f"{tid}:{s.get('lang', 'eng')}",
                '--default-track-flag', f"{tid}:{1 if s.get('default') else 0}",
                '--forced-display-flag', f"{tid}:{1 if s.get('forced') else 0}"]
        if s.get('name'):
            cmd += ['--track-name', f"{tid}:{s['name']}"]

    cmd += ['--language', f"{spec['video']}:und", src]

    order = [f"0:{spec['video']}", f"0:{spec['audio_default']}"]
    if spec.get('audio_secondary') is not None:
        order.append(f"0:{spec['audio_secondary']}")
    order += [f"0:{s['source_track']}" for s in internal]

    # Each external file is its own mkvmerge input, numbered from 1.
    for n, s in enumerate(external, start=1):
        cmd += ['--language', f"0:{s.get('lang', 'eng')}",
                '--default-track-flag', f"0:{1 if s.get('default') else 0}",
                '--forced-display-flag', f"0:{1 if s.get('forced') else 0}",
                '--track-name', f"0:{s.get('name', 'English')}",
                '--sub-charset', '0:UTF-8',
                s['file']]
        order.append(f'{n}:0')

    cmd += ['--track-order', ','.join(order)]
    return cmd


def run_mkvmerge(cmd, label):
    # 0 = ok, 1 = warnings (output still written), >=2 = fatal.
    # Check-and-register under the same lock the signal handler snapshots under: a
    # process is either already in the snapshot the handler kills, or created after
    # the flag was set (and refused here) — no gap where a new mkvmerge slips through.
    with _live_lock:
        if shutdown_requested:
            raise InterruptedError('shutdown requested before mkvmerge started')
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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


def regen_metadata(dst_folder, dst_mkv, meta, poster_url, fanart_url, imdbid, label):
    """NFO, artwork, and MKV tags rebuilt from TMDB — never copied from source."""
    write_nfo(meta, imdbid, os.path.join(dst_folder, 'movie.nfo'))
    try:
        download_image(poster_url, os.path.join(dst_folder, 'poster.jpg'))
        download_image(fanart_url, os.path.join(dst_folder, 'fanart.jpg'))
    except Exception as e:
        log(f"  ⚠️ {label}: artwork download failed: {e}")
    tags_path = os.path.join(dst_folder, 'tags.xml')
    write_tags_xml(meta, imdbid, tags_path)
    set_tags_in_mkv(dst_mkv, tags_path)
    os.remove(tags_path)
    log(f"  {label}: NFO/artwork/tags regenerated from TMDB")


def _discard_partial(dst, dst_folder):
    """Remove the file this run was writing; drop the folder only if it is now empty."""
    try:
        if dst and os.path.exists(dst):
            os.remove(dst)
        if dst_folder and os.path.isdir(dst_folder) and not os.listdir(dst_folder):
            os.rmdir(dst_folder)
    except OSError as e:
        log(f"  ⚠️ could not remove partial output {dst}: {e}")


def process(spec, apply_changes):
    label = spec['folder']
    t0 = time.perf_counter()
    partial_dst = partial_folder = None
    src_folder = spec['src_folder']
    try:
        if shutdown_requested:
            return {'folder': label, 'ok': False, 'error': 'not started', 'not_started': True}
        mkvs = [f for f in os.listdir(src_folder) if f.lower().endswith('.mkv')]
        if len(mkvs) != 1:
            return {'folder': label, 'ok': False, 'error': f'{len(mkvs)} MKVs in source folder'}
        src = os.path.join(src_folder, mkvs[0])

        info = mkv_identify(src)
        problems = validate(spec, info)
        if problems:
            return {'folder': label, 'ok': False, 'error': 'spec mismatch: ' + '; '.join(problems)}

        meta = poster_url = fanart_url = None
        if spec.get('imdb'):
            meta, poster_url, fanart_url = fetch_tmdb_metadata(spec['imdb'])

        # Output name computed from the actual selected tracks (same logic as
        # the English pipeline's enhanced_file_name) — spec may override.
        tracks = {t['id']: t for t in info.get('tracks') or []}
        if spec.get('output_name'):
            out_name = spec['output_name']
        elif meta:
            out_name = enhanced_file_name(meta, tracks[spec['video']],
                                          tracks[spec['audio_default']], '.mkv')
        else:
            return {'folder': label, 'ok': False,
                    'error': 'no imdb in spec and no output_name override — cannot name output'}

        dst_folder = os.path.join(CLEANED_DIR, label)
        dst = os.path.join(dst_folder, out_name)
        cmd = build_cmd(spec, src, dst)

        if not apply_changes:
            log(f"[DRY-RUN] {label} → {out_name}\n    {' '.join(cmd)}")
            return {'folder': label, 'ok': True, 'dry_run': True, 'cmd': cmd,
                    'output_name': out_name}

        os.makedirs(dst_folder, exist_ok=True)
        partial_dst, partial_folder = dst, dst_folder
        log(f"▶ {label}: remuxing → {dst}")
        run_mkvmerge(cmd, label)

        # Embedded MKV tags are the authoritative identity for each title.
        if meta:
            try:
                regen_metadata(dst_folder, dst, meta, poster_url, fanart_url,
                               spec['imdb'], label)
            except Exception as e:
                log(f"  ⚠️ {label}: metadata regeneration failed: {e}")

        elapsed = time.perf_counter() - t0
        size = os.path.getsize(dst)
        log(f"✔ {label}: done in {elapsed:.0f}s → {size/1024**3:.1f} GB")
        return {'folder': label, 'ok': True, 'output': dst,
                'size_gb': round(size / 1024**3, 1), 'seconds': round(elapsed)}

    except InterruptedError:
        # run_mkvmerge refused to start (flag already set): nothing was written by
        # this run, so there is nothing to discard — a pre-existing output for this
        # folder from an earlier completed run must survive untouched.
        return {'folder': label, 'ok': False, 'error': 'not started', 'not_started': True}
    except subprocess.TimeoutExpired:
        _discard_partial(partial_dst, partial_folder)
        return {'folder': label, 'ok': False,
                'error': f'mkvmerge exceeded {REMUX_TIMEOUT}s — investigate pool health, do not simply retry'}
    except subprocess.CalledProcessError as e:
        _discard_partial(partial_dst, partial_folder)
        if shutdown_requested:
            return {'folder': label, 'ok': False, 'error': 'interrupted', 'interrupted': True}
        detail = ((e.output or '') + (e.stderr or '')).strip()[:400]
        return {'folder': label, 'ok': False, 'error': f'mkvmerge failed: {detail}'}
    except Exception as e:
        _discard_partial(partial_dst, partial_folder)
        if shutdown_requested:
            return {'folder': label, 'ok': False, 'error': 'interrupted', 'interrupted': True}
        log(f"❌ {label}: {traceback.format_exc()}")
        return {'folder': label, 'ok': False, 'error': str(e)}


def metadata_refresh(spec):
    """Bring an already-remuxed film in CLEANED_DIR up to the current metadata
    standard: NFO/artwork/tags regenerated from TMDB, filename checked against
    the computed name. Reports a name mismatch; does not rename."""
    label = spec['folder']
    dst_folder = os.path.join(CLEANED_DIR, label)
    try:
        mkvs = [f for f in os.listdir(dst_folder) if f.lower().endswith('.mkv')]
        if len(mkvs) != 1:
            return {'folder': label, 'ok': False, 'error': f'{len(mkvs)} MKVs in {dst_folder}'}
        dst = os.path.join(dst_folder, mkvs[0])

        meta, poster_url, fanart_url = fetch_tmdb_metadata(spec['imdb'])
        regen_metadata(dst_folder, dst, meta, poster_url, fanart_url, spec['imdb'], label)

        info = mkv_identify(dst)
        tracks = info.get('tracks') or []
        video = next(t for t in tracks if t['type'] == 'video')
        audio = next(t for t in tracks if t['type'] == 'audio'
                     and (t.get('properties') or {}).get('default_track'))
        expected = enhanced_file_name(meta, video, audio, '.mkv')
        return {'folder': label, 'ok': True, 'filename': mkvs[0],
                'computed': expected, 'name_match': expected == mkvs[0]}
    except Exception as e:
        log(f"❌ {label}: {traceback.format_exc()}")
        return {'folder': label, 'ok': False, 'error': str(e)}


def _summarise(results, not_started):
    """Log the four groups and return the exit code."""
    print()
    ok = [r for r in results if r['ok']]
    interrupted = [r for r in results if r.get('interrupted')]
    # A future can be cancelled before process() ever runs (name lands in `not_started`
    # via main()'s sweep) or process() can bail out at entry because the flag was
    # already set (its result dict carries 'not_started' instead). Both mean the same
    # thing to the summary, so fold the dict form's folder name into the same group.
    not_started_results = [r for r in results if r.get('not_started')]
    failed = [r for r in results
              if not r['ok'] and not r.get('interrupted') and not r.get('not_started')]
    all_not_started = sorted(not_started) + sorted(r['folder'] for r in not_started_results)
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
    for name in sorted(all_not_started):
        log(f"⏸ {name}: not started")
    log(f"{len(ok)}/{len(results) + len(not_started)} succeeded"
        + (f", {len(failed)} failed" if failed else "")
        + (f", {len(interrupted)} interrupted" if interrupted else "")
        + (f", {len(all_not_started)} not started" if all_not_started else ""))
    # A Ctrl-C that lands with no mkvmerge live yet (pre-remux stage, or --metadata-only
    # between films) can otherwise produce zero interrupted/not-started entries — the
    # flag alone must still force 130, or a genuinely-interrupted run could exit 0/1.
    if interrupted or not_started or not_started_results or shutdown_requested:
        return 130
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('spec', nargs='+', help='JSON spec file(s) describing the films')
    ap.add_argument('--run', action='store_true', help='Execute (default is dry-run)')
    ap.add_argument('--metadata-only', action='store_true',
                    help='Skip remux; regenerate NFO/artwork/tags on films already in CLEANED_DIR')
    ap.add_argument('--workers', type=int, default=3)
    args = ap.parse_args()

    specs = []
    for path in args.spec:
        with open(path) as f:
            specs.extend(json.load(f))

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
    cancelled = set()
    swept = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, s, args.run): s['folder'] for s in specs}
        for fut in as_completed(futures):
            if shutdown_requested and not swept:
                # Cancel whatever is still pending, exactly once. Futures already
                # running keep going (the signal handler is killing their mkvmerge
                # directly) and report their own 'interrupted' result when they finish.
                swept = True
                for f, name in futures.items():
                    if name not in cancelled and f.cancel():
                        cancelled.add(name)
                        not_started.append(name)
            if fut.cancelled():
                # Already accounted for in not_started above — collecting its result
                # would raise CancelledError and double-count the film as failed too.
                continue
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({'folder': futures[fut], 'ok': False,
                                'error': f'{type(e).__name__}: {e}'})
    return _summarise(results, not_started)


if __name__ == '__main__':
    sys.exit(main())
