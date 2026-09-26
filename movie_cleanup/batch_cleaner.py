#!/usr/bin/env python3

import os, shutil, time, traceback, json, subprocess, re, signal, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from xml.etree.ElementTree import Element, SubElement, ElementTree
from dotenv import load_dotenv

load_dotenv()

# ========== CONFIG =============
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
SOURCE_DIR = os.path.join(SERVARR_DIR, 'movies')
DEST_DIR = os.path.join(SERVARR_DIR, 'tagged')
REVIEW_DIR = os.path.join(SERVARR_DIR, 'review')
FAILED_DIR = os.path.join(SERVARR_DIR, 'failed')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
MAX_WORKERS = 4
TMDB_API_KEY = os.getenv('TMDB_API_KEY', 'your_api_key_here')

IMDB_RE = re.compile(r'tt\d{6,9}')
VIDEO_EXTS = ('.mkv', '.mp4')

# Global flag for graceful shutdown
shutdown_requested = False

def signal_handler(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    log("⚠️ Interrupt signal received, initiating graceful shutdown...")
    import threading
    def force_exit():
        import time
        time.sleep(3)
        log("⚠️ Force exiting due to multiple interrupts...")
        os._exit(1)
    threading.Thread(target=force_exit, daemon=True).start()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def log(msg):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {msg}"
    print(line)
    with open(os.path.join(LOG_DIR, 'batch_cleaner_debug.log'), 'a') as f:
        f.write(line + '\n')

def timed(msg, func, *args, **kwargs):
    log(f"START: {msg}")
    start = time.perf_counter()
    try:
        result = func(*args, **kwargs)
    except Exception as e:
        log(f"ERROR in {msg}: {e}\n{traceback.format_exc()}")
        raise
    elapsed = time.perf_counter() - start
    log(f"DONE: {msg} ({elapsed:.2f}s)")
    return result

def fast_copy(src, dst):
    subprocess.run(['cp', '--reflink=auto', src, dst], check=True, timeout=300)

def find_imdbid(folder, filename):
    # Radarr's naming template embeds [imdbid-tt...] directly in the file.
    if filename:
        m = IMDB_RE.search(filename)
        if m:
            return m.group(0)

    nfo = os.path.join(folder, 'movie.nfo')
    if os.path.isfile(nfo):
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(nfo)
            for uid in tree.getroot().findall('uniqueid'):
                if uid.get('type') == 'imdb' and uid.text:
                    m = IMDB_RE.match(uid.text.strip())
                    if m:
                        return m.group(0)
        except Exception:
            pass
        try:
            with open(nfo, 'r', encoding='utf-8', errors='ignore') as fh:
                m = IMDB_RE.search(fh.read())
                if m:
                    return m.group(0)
        except Exception:
            pass

    return None

def _tmdb_get(url):
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        # requests error messages embed the full URL (api_key included) and
        # would otherwise land verbatim in the debug log. Redact the key.
        raise Exception(str(e).replace(TMDB_API_KEY, '<TMDB_API_KEY>')) from None

def fetch_tmdb_metadata(imdbid):
    url = f'https://api.themoviedb.org/3/find/{imdbid}?api_key={TMDB_API_KEY}&external_source=imdb_id'
    resp = _tmdb_get(url)
    data = resp.json()
    # Movie results only: TMDB movie and TV ids are independent namespaces, so
    # feeding a tv_results id to the /movie/ endpoint can bind the file to a
    # completely unrelated film. An IMDb id that only resolves as TV is not a
    # movie — fail it out to ./failed/ instead.
    movie_results = data.get('movie_results') or []
    if not movie_results:
        raise Exception(f"TMDB lookup for {imdbid} returned no movie results"
                        + (" (matched a TV entry — not a movie)" if data.get('tv_results') else ""))
    tmdb_id = movie_results[0]['id']
    url = f'https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={TMDB_API_KEY}&append_to_response=credits,belongs_to_collection,release_dates'
    meta = _tmdb_get(url).json()
    poster_url = 'https://image.tmdb.org/t/p/original' + meta['poster_path'] if meta.get('poster_path') else ''
    fanart_url = 'https://image.tmdb.org/t/p/original' + meta['backdrop_path'] if meta.get('backdrop_path') else ''
    return meta, poster_url, fanart_url

def download_image(url, dest):
    if not url:
        return
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    with open(dest, 'wb') as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)

def strip_attachments(mkvfile):
    # 300s (not the old 30s): the pool is storage-bound, and this read can stall
    # behind concurrent big I/O (other workers' fast_copy, or a manual remux job)
    # even though it's just a metadata read, not a full remux.
    result = subprocess.run(['mkvmerge', '-J', mkvfile],
                            capture_output=True, text=True, check=True, timeout=300)
    info = json.loads(result.stdout)
    attachments = info.get('attachments', [])
    if not attachments:
        return
    cmd = ['mkvpropedit', mkvfile]
    for att in attachments:
        uid = (att.get('properties') or {}).get('uid')
        if uid is not None:
            cmd += ['--delete-attachment', f'={uid}']
    if len(cmd) > 2:
        subprocess.run(cmd, check=True, timeout=60)

def write_json(meta, dest):
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

def pick_us_certification(meta):
    for result in (meta.get('release_dates') or {}).get('results', []):
        if result.get('iso_3166_1') != 'US':
            continue
        for rd in result.get('release_dates', []):
            cert = (rd.get('certification') or '').strip()
            if cert:
                return cert
    return ''

def sort_title(title):
    for article in ('The ', 'A ', 'An '):
        if title.startswith(article):
            return f"{title[len(article):]}, {article.strip()}"
    return title

def write_tags_xml(meta, imdbid, dest):
    root = Element("Tags")
    tag = SubElement(root, "Tag")
    targets = SubElement(tag, "Targets")
    SubElement(targets, "TargetTypeValue").text = "50"

    tags_data = [
        ("TITLE", meta.get("title") or meta.get("name")),
        ("YEAR", (meta.get('release_date') or '')[:4]),
        ("DIRECTOR", next((c['name'] for c in meta.get('credits', {}).get('crew', []) if c.get('job') == 'Director'), "")),
        ("GENRE", ", ".join([g['name'] for g in meta.get('genres', [])])),
        ("IMDB", imdbid),
        ("TMDB", str(meta.get("id"))),
        ("PLOT", meta.get("overview", "")),
    ]

    collection = meta.get('belongs_to_collection')
    if collection:
        tags_data.extend([
            ("COLLECTION", collection.get('name', '')),
            ("COLLECTION_ID", str(collection.get('id', ''))),
        ])

    for k, v in tags_data:
        if v:
            s = SubElement(tag, "Simple")
            SubElement(s, "Name").text = k
            SubElement(s, "String").text = v

    ElementTree(root).write(dest, encoding="utf-8", xml_declaration=True)

def set_tags_in_mkv(mkvfile, tags_xml):
    subprocess.run(['mkvpropedit', mkvfile, '--tags', f'all:{tags_xml}'], check=True, timeout=60)

def write_nfo(meta, imdbid, dest):
    root = Element('movie')

    def safe_sub(parent, tag, val):
        if val:
            SubElement(parent, tag).text = val

    title = meta.get('title')
    if title:
        title = title.replace('_', ' ')
        title = re.sub(r'\s*\(\d{4}\)$', '', title)

    safe_sub(root, 'title', title)
    safe_sub(root, 'originaltitle', meta.get('original_title'))
    if title:
        safe_sub(root, 'sorttitle', sort_title(title))
    safe_sub(root, 'year', (meta.get('release_date') or '')[:4])
    safe_sub(root, 'premiered', meta.get('release_date'))
    safe_sub(root, 'plot', meta.get('overview'))
    safe_sub(root, 'tagline', meta.get('tagline'))
    safe_sub(root, 'runtime', str(meta.get('runtime') or ''))
    safe_sub(root, 'mpaa', pick_us_certification(meta))
    safe_sub(root, 'rating', str(meta.get('vote_average') or ''))
    safe_sub(root, 'votes', str(meta.get('vote_count') or ''))
    safe_sub(root, 'tmdbid', str(meta.get('id')))
    safe_sub(root, 'imdbid', imdbid)

    if meta.get('id'):
        uid = SubElement(root, 'uniqueid')
        uid.set('type', 'tmdb')
        uid.set('default', 'true')
        uid.text = str(meta['id'])
    if imdbid:
        uid = SubElement(root, 'uniqueid')
        uid.set('type', 'imdb')
        uid.text = imdbid

    for g in meta.get('genres', []):
        safe_sub(root, 'genre', g.get('name'))

    for studio in meta.get('production_companies', []):
        safe_sub(root, 'studio', studio.get('name'))

    directors = [c['name'] for c in meta.get('credits', {}).get('crew', []) if c.get('job') == 'Director']
    for director in directors:
        safe_sub(root, 'director', director)

    for i, a in enumerate(meta.get('credits', {}).get('cast', [])[:8]):
        actor_el = SubElement(root, 'actor')
        safe_sub(actor_el, 'name', a.get('name'))
        safe_sub(actor_el, 'role', a.get('character'))
        safe_sub(actor_el, 'order', str(i))
        profile = a.get('profile_path')
        if profile:
            safe_sub(actor_el, 'thumb', f'https://image.tmdb.org/t/p/original{profile}')

    for country in meta.get('production_countries', []):
        safe_sub(root, 'country', country.get('name'))

    collection = meta.get('belongs_to_collection')
    if collection:
        safe_sub(root, 'set', collection.get("name"))
        safe_sub(root, 'setid', str(collection.get("id")))
        safe_sub(root, 'setoverview', collection.get("overview"))

    ElementTree(root).write(dest, encoding="utf-8", xml_declaration=True)

def _normalize_title_for_path(title):
    safe = re.sub(r'[\\/:*?"<>|]', '', title)
    safe = safe.replace(' ', '_').replace('.', '').strip()
    safe = re.sub(r'_+', '_', safe).strip('_')
    return safe

def clean_folder_name(meta):
    title = meta.get('title') or meta.get('name') or "Unknown"
    year = (meta.get('release_date') or '')[:4]
    safe_title = _normalize_title_for_path(title)
    if year:
        return f"{safe_title}_({year})"
    return safe_title

def clean_file_name(meta, ext):
    title = meta.get('title') or meta.get('name') or "Unknown"
    year = (meta.get('release_date') or '')[:4]
    safe_title = _normalize_title_for_path(title)
    if year:
        return f"{safe_title}_({year}){ext}"
    return f"{safe_title}{ext}"

def _move_to_failed(src_folder, base):
    failed = os.path.join(FAILED_DIR, base)
    try:
        # A leftover failed/<base> from a prior run would make shutil.move
        # nest the folder inside it (failed/Base/Base). Replace it instead.
        if os.path.exists(failed):
            shutil.rmtree(failed, ignore_errors=True)
        shutil.move(src_folder, failed)
        log(f"  [FAILED] Moved to failed directory: {failed}")
    except Exception as e:
        log(f"❌ Could not move to failed: {e}")

def _move_to_review(src_folder, base):
    review = os.path.join(REVIEW_DIR, base)
    try:
        if os.path.exists(review):
            log(f"  [REVIEW] Target already exists: {review} — leaving source in place")
            return
        shutil.move(src_folder, review)
        log(f"  [REVIEW] Moved to review directory: {review}")
    except Exception as e:
        log(f"❌ Could not move to review: {e}")

# Destination folders claimed this run (dedupe guard across workers).
_claim_lock = threading.Lock()
_claimed_dsts = set()

def clean_folder(src_folder):
    global shutdown_requested
    if shutdown_requested:
        return

    base = os.path.basename(src_folder)
    log(f"\n▶ Processing: {base}")
    dst_folder = None
    try:
        t0 = time.perf_counter()

        videos = [f for f in os.listdir(src_folder) if f.lower().endswith(VIDEO_EXTS)]

        if len(videos) > 1:
            log(f"[REVIEW] {len(videos)} video files in {base} — needs human review")
            _move_to_review(src_folder, base)
            return

        if not videos:
            log(f"❌ [SKIP] No video files in {base}")
            _move_to_failed(src_folder, base)
            return

        video = videos[0]
        src_video = os.path.join(src_folder, video)

        # MP4 is not an accepted source container — flag and fail any .mp4.
        if not video.lower().endswith('.mkv'):
            log(f"❌ [FAILED] MP4 source not accepted: {video} — moving to failed")
            _move_to_failed(src_folder, base)
            return

        imdbid = find_imdbid(src_folder, video)
        log(f"  [IMDB] Found IMDb ID: {imdbid}")

        if not imdbid:
            log(f"❌ [SKIP] No IMDb ID found in {base}. Moving to failed.")
            _move_to_failed(src_folder, base)
            return

        meta, poster_url, fanart_url = timed(f"TMDb lookup for {imdbid}", fetch_tmdb_metadata, imdbid)

        # Foreign-original films are not handled by this pipeline — they are
        # managed by separate scripts / manual processes. Flag and fail them.
        if meta.get('original_language') != 'en':
            log(f"❌ [FAILED] Foreign original (original_language="
                f"{meta.get('original_language')}) — not handled here, moving to failed")
            _move_to_failed(src_folder, base)
            return

        new_base = clean_folder_name(meta)
        dst_folder = os.path.join(DEST_DIR, new_base)

        # Two source folders can resolve to the same movie (e.g. an upgrade
        # sitting next to the original). Without this claim, both workers would
        # fast_copy to the same destination file concurrently (corrupt output)
        # and either error handler could rmtree the shared folder.
        with _claim_lock:
            if dst_folder in _claimed_dsts:
                log(f"[REVIEW] {base}: duplicate of an in-flight/processed title "
                    f"({new_base}) — needs human review")
                _move_to_review(src_folder, base)
                return
            _claimed_dsts.add(dst_folder)

        os.makedirs(dst_folder, exist_ok=True)
        dst_mkv = os.path.join(dst_folder, clean_file_name(meta, '.mkv'))

        # Source is guaranteed .mkv here (MP4 was failed at intake above).
        timed(f"Copy MKV: {src_video} → {dst_mkv}", fast_copy, src_video, dst_mkv)

        timed(f"Strip attachments: {dst_mkv}", strip_attachments, dst_mkv)

        timed("Download poster", download_image, poster_url, os.path.join(dst_folder, "poster.jpg"))
        timed("Download fanart", download_image, fanart_url, os.path.join(dst_folder, "fanart.jpg"))
        timed("Write metadata.json", write_json, meta, os.path.join(dst_folder, "metadata.json"))
        tags_path = os.path.join(dst_folder, "tags.xml")
        timed("Write tags.xml", write_tags_xml, meta, imdbid, tags_path)
        timed("Inject tags.xml into MKV", set_tags_in_mkv, dst_mkv, tags_path)
        timed("Write movie.nfo", write_nfo, meta, imdbid, os.path.join(dst_folder, "movie.nfo"))

        # Source deletion is post-success cleanup. If it fails (permissions,
        # partial rmtree), the completed output in tagged/ is still good —
        # never tear it down or send the half-deleted source to failed/,
        # which could leave no intact copy of the movie at all.
        try:
            timed(f"Delete original folder: {src_folder}", shutil.rmtree, src_folder)
        except Exception as e:
            log(f"⚠ [WARN] {base}: output complete, but could not delete source "
                f"{src_folder}: {e} — remove it manually")
        log(f"✔ [DONE] {base} total {(time.perf_counter()-t0):.2f}s\n")

    except Exception as e:
        log(f"❌ ERROR processing {base}: {e}\n{traceback.format_exc()}")
        if dst_folder and os.path.isdir(dst_folder):
            shutil.rmtree(dst_folder, ignore_errors=True)
        if os.path.isdir(src_folder):
            _move_to_failed(src_folder, base)

def main():
    global shutdown_requested

    for d in (LOG_DIR, DEST_DIR, REVIEW_DIR, FAILED_DIR):
        os.makedirs(d, exist_ok=True)

    srcs = [os.path.join(SOURCE_DIR, d) for d in os.listdir(SOURCE_DIR)
            if os.path.isdir(os.path.join(SOURCE_DIR, d)) and not d.startswith('.')]
    log(f"▶ Queued {len(srcs)} folders for cleaning with up to {MAX_WORKERS} workers")

    if srcs:
        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = [pool.submit(clean_folder, src) for src in srcs]
                completed_count = 0

                try:
                    for future in as_completed(futures):
                        completed_count += 1
                        log(f"📊 Progress: {completed_count}/{len(futures)} folders completed")

                        if shutdown_requested:
                            log("⚠️ Shutdown requested, cancelling remaining tasks...")
                            for f in futures:
                                f.cancel()
                            pool.shutdown(wait=False)
                            log("⚠️ Shutdown complete. Some folders may not have been processed.")
                            return

                        try:
                            future.result(timeout=300)
                        except Exception as e:
                            log(f"❌ Worker thread error: {e}")
                            log(f"❌ Worker thread traceback: {traceback.format_exc()}")

                except KeyboardInterrupt:
                    log("⚠️ Received interrupt signal, shutting down gracefully...")
                    shutdown_requested = True
                    for f in futures:
                        f.cancel()
                    pool.shutdown(wait=False)
                    log("⚠️ Shutdown complete. Some folders may not have been processed.")
                    return

        except KeyboardInterrupt:
            log("⚠️ Received interrupt signal during startup, exiting...")
            return

    log("All folders processed.")


if __name__ == "__main__":
    main()
