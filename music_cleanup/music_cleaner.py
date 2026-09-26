#!/usr/bin/env python3
"""Music pipeline, end to end: music/ -> cleaned_music/ -> $RC_MEDIA_BASE/music/.

Per album: convert lossless sources (FLAC/APE/WavPack) to ALAC, carry every tag across
using MusicBrainz Picard's MP4 mapping, verify decoded-PCM equality, remove the source,
then publish: fix permissions (sudo fix_media_perms.py), move into the music folder, and
unmonitor + delete the album's track records in Lidarr. Lossy MP3/AAC copy through.
Stage dirs live under $RC_SERVARR_DIR (default $RC_MEDIA_BASE/servarr = /storage/media/servarr); CWD is irrelevant.

  --no-publish     stop at cleaned_music/
  --publish-only   skip cleaning; publish what is already in cleaned_music/
  --no-lidarr      publish but leave Lidarr alone
  --library DIR    destination (default $RC_MEDIA_BASE/music)
"""

import argparse, os, re, shutil, signal, subprocess, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import mutagen
import requests
from mutagen.easyid3 import EasyID3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

load_dotenv()

# ========== CONFIG =============
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
SOURCE_DIR = os.path.join(SERVARR_DIR, 'music')
DEST_DIR = os.path.join(SERVARR_DIR, 'cleaned_music')
REVIEW_DIR = os.path.join(SERVARR_DIR, 'review_music')
FAILED_DIR = os.path.join(SERVARR_DIR, 'failed_music')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
MAX_WORKERS = 4
FFMPEG = os.getenv('RC_FFMPEG_BIN', 'ffmpeg')
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX_PERMS = os.path.join(REPO, 'maintenance', 'fix_media_perms.py')
# Lidarr refuses to delete track-file records (HTTP 409 "root folder is empty") when its
# root has no subdirectories, and its own delete-empty-folders setting removes the artist
# folder mid-operation. A permanent placeholder directory keeps the guard satisfied.
LIDARR_KEEP_DIR = os.path.join(SOURCE_DIR, '.lidarr_keep')

OPTS = argparse.Namespace(publish=True, clean=True, lidarr=True, library=None, skip_perms=False)

MBID_RE = re.compile(r'_\[mbid-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]', re.IGNORECASE)
CONVERT_EXTS = {'.flac', '.ape', '.wv'}
COPY_EXTS = {'.mp3', '.m4a', '.aac'}
AUDIO_EXTS = CONVERT_EXTS | COPY_EXTS | {'.ogg', '.opus', '.wma', '.wav', '.aiff', '.aif', '.mka', '.dsf', '.mpc'}

# Vorbis comment -> MP4 atom (MusicBrainz Picard mapping)
TEXT_MAP = {
    'albumartist': 'aART', 'artist': '\xa9ART', 'album': '\xa9alb', 'title': '\xa9nam',
    'date': '\xa9day', 'genre': '\xa9gen', 'composer': '\xa9wrt', 'comment': '\xa9cmt',
    'albumsort': 'soal', 'albumartistsort': 'soaa', 'artistsort': 'soar', 'titlesort': 'sonm',
}
FREEFORM_MAP = {
    'originaldate': 'RELEASEDATE',
    'label': 'LABEL', 'media': 'MEDIA', 'catalognumber': 'CATALOGNUMBER',
    'releasetype': 'MusicBrainz Album Type', 'musicbrainz_albumtype': 'MusicBrainz Album Type',
    'releasestatus': 'MusicBrainz Album Status', 'musicbrainz_albumstatus': 'MusicBrainz Album Status',
    'releasecountry': 'MusicBrainz Album Release Country',
    'musicbrainz_albumid': 'MusicBrainz Album Id',
    'musicbrainz_releasegroupid': 'MusicBrainz Release Group Id',
    'musicbrainz_artistid': 'MusicBrainz Artist Id',
    'musicbrainz_albumartistid': 'MusicBrainz Album Artist Id',
    'musicbrainz_trackid': 'MusicBrainz Track Id',
    'musicbrainz_releasetrackid': 'MusicBrainz Release Track Id',
    'isrc': 'ISRC', 'barcode': 'BARCODE',
}
FF = '----:com.apple.iTunes:'
VERIFY_FREEFORM = ('MusicBrainz Release Group Id', 'MusicBrainz Album Id', 'MusicBrainz Artist Id',
                   'MusicBrainz Album Artist Id', 'MusicBrainz Track Id', 'MusicBrainz Release Track Id')

shutdown_requested = False


def signal_handler(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    log("⚠️ Interrupt signal received, initiating graceful shutdown...")
    import threading
    def force_exit():
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
    with open(os.path.join(LOG_DIR, 'music_cleaner_debug.log'), 'a') as f:
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
    subprocess.run(['cp', '--reflink=auto', src, dst], check=True, timeout=600)


class Reject(Exception):
    """Album cannot be processed; `stage` is REVIEW_DIR or FAILED_DIR."""
    def __init__(self, stage, reason):
        super().__init__(reason)
        self.stage = stage


# ---------- helpers ----------

def strip_mbid(name):
    return MBID_RE.sub('', name)


def folder_mbid(name):
    m = MBID_RE.search(name)
    return m.group(1).lower() if m else None


def first(tags, key):
    v = tags.get(key)
    if not v:
        return None
    return str(v[0])


def values(tags, key):
    v = tags.get(key)
    return [str(x) for x in v] if v else []


def pcm_md5(path):
    r = subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', path,
                        '-map', '0:a:0', '-vn', '-af', 'aformat=sample_fmts=s32', '-f', 'md5', '-'],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not r.stdout.startswith('MD5='):
        raise RuntimeError(f"ffmpeg hash failed for {os.path.basename(path)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def source_bits(audio):
    bps = getattr(audio.info, 'bits_per_sample', 16) or 16
    return 24 if bps > 16 else 16


def convert_to_alac(src, dst, bits):
    fmt = 's16p' if bits == 16 else 's32p'
    r = subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
                        '-map', '0:a:0', '-vn', '-c:a', 'alac', '-sample_fmt', fmt,
                        '-map_metadata', '-1', '-movflags', '+faststart', dst],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg alac failed: {r.stderr.strip()[:300]}")


def source_picture(audio):
    pics = getattr(audio, 'pictures', None) or []
    if not pics and audio.tags is not None:
        # APE / WavPack (APEv2) store cover as a binary item
        for k in ('Cover Art (Front)', 'Cover Art (front)'):
            if k in audio.tags:
                data = bytes(audio.tags[k].value)
                if b'\x00' in data:
                    data = data.split(b'\x00', 1)[1]
                return data, ('image/png' if data[:8] == b'\x89PNG\r\n\x1a\n' else 'image/jpeg')
        return None
    p = next((p for p in pics if p.type == 3), pics[0]) if pics else None
    return (p.data, p.mime) if p else None


def write_mp4_tags(dst, src_tags, picture):
    mp4 = MP4(dst)
    if mp4.tags is None:
        mp4.add_tags()
    t = mp4.tags
    lower = {k.lower(): v for k, v in src_tags.items()}
    for vk, atom in TEXT_MAP.items():
        vals = values(lower, vk)
        if vals:
            t[atom] = ['; '.join(vals)] if atom != '\xa9gen' else vals
    if '\xa9day' not in t:
        for alt in ('originaldate', 'year', 'originalyear'):
            if values(lower, alt):
                t['\xa9day'] = [values(lower, alt)[0]]
                break
    written = set()
    for vk, name in FREEFORM_MAP.items():
        vals = values(lower, vk)
        if vals and name not in written:
            t[FF + name] = [MP4FreeForm(v.encode('utf-8')) for v in vals]
            written.add(name)
    tn = first(lower, 'tracknumber'); tt = first(lower, 'tracktotal') or first(lower, 'totaltracks')
    dn = first(lower, 'discnumber'); dt = first(lower, 'disctotal') or first(lower, 'totaldiscs')
    if tn:
        t['trkn'] = [(int(tn.split('/')[0]), int(tt) if tt else 0)]
    if dn:
        t['disk'] = [(int(dn.split('/')[0]), int(dt) if dt else 0)]
    if (first(lower, 'albumartist') or '').lower() == 'various artists' or first(lower, 'compilation') == '1':
        t['cpil'] = True
    if picture:
        data, mime = picture
        fmt = MP4Cover.FORMAT_PNG if mime == 'image/png' else MP4Cover.FORMAT_JPEG
        t['covr'] = [MP4Cover(data, imageformat=fmt)]
    mp4.save()


def verify_output(src_path, dst_path, src_tags, had_picture, src_md5):
    if pcm_md5(dst_path) != src_md5:
        raise RuntimeError("PCM hash mismatch")
    t = MP4(dst_path).tags
    lower = {k.lower(): v for k, v in src_tags.items()}
    for vk, atom in (('title', '\xa9nam'), ('album', '\xa9alb'), ('albumartist', 'aART')):
        want = '; '.join(values(lower, vk))
        got = (t.get(atom) or [''])[0]
        if want != got:
            raise RuntimeError(f"tag mismatch {vk}: {want!r} != {got!r}")
    for vk, name in FREEFORM_MAP.items():
        if name in VERIFY_FREEFORM and vk in lower:
            want = values(lower, vk)
            got = [bytes(b).decode('utf-8') for b in (t.get(FF + name) or [])]
            if want != got:
                raise RuntimeError(f"tag mismatch {name}: {want} != {got}")
    if had_picture != bool(t.get('covr')):
        raise RuntimeError("cover art mismatch")


def _output_ok(dst, src_tags):
    """Resume check: trust an existing .m4a only if it opens and its recording id matches."""
    try:
        t = MP4(dst).tags
    except Exception:
        return False
    want = values({k.lower(): v for k, v in src_tags.items()}, 'musicbrainz_trackid')
    got = [bytes(b).decode('utf-8') for b in (t.get(FF + 'MusicBrainz Track Id') or [])]
    return bool(want) and want == got


def _move_album(album_dir, artist_name, stage):
    dest_artist = os.path.join(stage, artist_name)
    os.makedirs(dest_artist, exist_ok=True)
    target = os.path.join(dest_artist, os.path.basename(album_dir))
    if os.path.exists(target):
        shutil.rmtree(target)
    shutil.move(album_dir, target)
    _rmdir_if_empty(os.path.dirname(album_dir))


def _rmdir_if_empty(d):
    try:
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    except OSError:
        pass


# ---------- publish ----------

FF_RG = FF + 'MusicBrainz Release Group Id'


def release_group_id(album_dir):
    for name in sorted(os.listdir(album_dir)):
        p = os.path.join(album_dir, name)
        ext = os.path.splitext(name)[1].lower()
        try:
            if ext == '.m4a':
                v = MP4(p).tags.get(FF_RG)
                if v:
                    return bytes(v[0]).decode('utf-8')
            elif ext == '.mp3':
                v = EasyID3(p).get('musicbrainz_releasegroupid')
                if v:
                    return v[0]
        except Exception:
            continue
    return None


class Lidarr:
    def __init__(self, url, key):
        self.base = url.rstrip('/') + '/api/v1'
        self.s = requests.Session()
        self.s.headers['X-Api-Key'] = key

    def album_by_rg(self, rg):
        r = self.s.get(f"{self.base}/album", params={'foreignAlbumId': rg}, timeout=30)
        r.raise_for_status()
        items = r.json()
        return items[0] if items else None

    def unmonitor(self, album):
        r = self.s.put(f"{self.base}/album/{album['id']}", json=dict(album, monitored=False), timeout=30)
        r.raise_for_status()

    def trackfile_ids(self, album_id):
        r = self.s.get(f"{self.base}/trackfile", params={'albumId': album_id}, timeout=30)
        r.raise_for_status()
        return [t['id'] for t in r.json()]

    def delete_trackfiles(self, ids):
        if ids:
            r = self.s.delete(f"{self.base}/trackfile/bulk", json={'trackFileIds': ids}, timeout=60)
            r.raise_for_status()


_lidarr = None


def lidarr_client():
    global _lidarr
    if _lidarr is None and OPTS.lidarr:
        url, key = os.getenv('LIDARR_URL'), os.getenv('LIDARR_API_KEY')
        if url and key:
            _lidarr = Lidarr(url, key)
    return _lidarr


def library_dir():
    return OPTS.library or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'music')


def _own_dir(path):
    """A directory we create must match fix_media_perms (owner/group when root, 2775)."""
    os.chmod(path, 0o2775)
    if os.geteuid() == 0:
        shutil.chown(path, os.getenv('RC_MEDIA_OWNER', 'david'), os.getenv('RC_MEDIA_GROUP', 'media'))


def lidarr_handoff(album_dir, label):
    rg = release_group_id(album_dir)
    if not rg:
        log(f"[WARN] {label}: no MusicBrainz Release Group Id in files; Lidarr handoff skipped")
        return
    client = lidarr_client()
    if not client:
        if OPTS.lidarr:
            log(f"[WARN] LIDARR_URL/LIDARR_API_KEY not set; {label} left monitored in Lidarr")
        return
    if os.path.isdir(SOURCE_DIR) and not os.path.isdir(LIDARR_KEEP_DIR):
        os.makedirs(LIDARR_KEEP_DIR)
        _own_dir(LIDARR_KEEP_DIR)
    a = client.album_by_rg(rg)
    if not a:
        log(f"[WARN] Lidarr has no album with release group {rg}")
        return
    if a.get('monitored'):
        client.unmonitor(a)
        log(f"Lidarr: unmonitored album {a['id']} ({a.get('title')})")
    ids = client.trackfile_ids(a['id'])
    client.delete_trackfiles(ids)
    log(f"Lidarr: deleted {len(ids)} track-file records for album {a['id']}")


def publish_album(cleaned_dir):
    """cleaned_music/<Artist>/<Album> -> <library>/<Artist>/<Album>, then Lidarr handoff."""
    artist = os.path.basename(os.path.dirname(cleaned_dir))
    album = os.path.basename(cleaned_dir)
    label = f"{artist}/{album}"
    target_artist = os.path.join(library_dir(), artist)
    target = os.path.join(target_artist, album)
    if os.path.exists(target):
        log(f"[SKIP] {label}: already in {library_dir()} — left in {DEST_DIR}")
        return False
    if not OPTS.skip_perms:
        cmd = [FIX_PERMS, '--apply', cleaned_dir]
        if os.geteuid() != 0:
            cmd = ['sudo', '-n', sys.executable] + cmd
        else:
            cmd = [sys.executable] + cmd
        timed(f"fix_media_perms {label}", subprocess.run, cmd, check=True, timeout=600,
              stdout=subprocess.DEVNULL)
    if not os.path.isdir(target_artist):
        os.makedirs(target_artist)
        _own_dir(target_artist)
    shutil.move(cleaned_dir, target)
    _rmdir_if_empty(os.path.dirname(cleaned_dir))
    log(f"published {label} -> {target}")
    lidarr_handoff(target, label)
    return True


# ---------- per-album work ----------

def inspect_album(album_dir):
    """Return (mode, files) or raise Reject. mode is 'convert' or 'copy'."""
    album_base = os.path.basename(album_dir)
    artist_base = os.path.basename(os.path.dirname(album_dir))
    rg = folder_mbid(album_base)
    if not rg or not folder_mbid(artist_base):
        raise Reject(FAILED_DIR, "folder name lacks [mbid-...] token")

    entries = sorted(os.listdir(album_dir))
    audio = [os.path.join(album_dir, e) for e in entries
             if os.path.splitext(e)[1].lower() in AUDIO_EXTS and os.path.isfile(os.path.join(album_dir, e))]
    if not audio:
        raise Reject(FAILED_DIR, "no audio files")
    exts = {os.path.splitext(p)[1].lower() for p in audio}
    if len(exts) > 1:
        raise Reject(REVIEW_DIR, f"mixed codecs {sorted(exts)}")
    ext = exts.pop()
    if ext in CONVERT_EXTS:
        mode = 'convert'
    elif ext in COPY_EXTS:
        mode = 'copy'
    else:
        raise Reject(FAILED_DIR, f"unsupported codec {ext}")

    files = []
    per_disc_total = {}
    for p in audio:
        a = mutagen.File(p)
        if a is None or a.tags is None:
            raise Reject(FAILED_DIR, f"unreadable tags: {os.path.basename(p)}")
        tags = {k.lower(): v for k, v in a.tags.items()} if mode == 'convert' else {}
        if mode == 'convert':
            file_rg = first(tags, 'musicbrainz_releasegroupid')
            if not file_rg:
                raise Reject(REVIEW_DIR, f"missing musicbrainz_releasegroupid: {os.path.basename(p)}")
            if file_rg.lower() != rg:
                raise Reject(REVIEW_DIR, f"releasegroupid {file_rg} != folder {rg}")
            disc = first(tags, 'discnumber') or '1'
            tt = first(tags, 'tracktotal') or first(tags, 'totaltracks')
            if tt:
                per_disc_total[disc.split('/')[0]] = int(tt)
        files.append((p, a, tags))

    if mode == 'convert' and per_disc_total:
        expected = sum(per_disc_total.values())
        if expected != len(files):
            raise Reject(REVIEW_DIR, f"track count {len(files)} != tracktotal {expected}")
    return mode, files


def clean_album(album_dir):
    global shutdown_requested
    if shutdown_requested:
        return
    album_base = os.path.basename(album_dir)
    artist_base = os.path.basename(os.path.dirname(album_dir))
    out_artist = strip_mbid(artist_base)
    out_album = strip_mbid(album_base)
    dest_dir = os.path.join(DEST_DIR, out_artist, out_album)
    log(f"▶ Processing: {artist_base}/{album_base}")

    try:
        mode, files = inspect_album(album_dir)
    except Reject as r:
        tag = '[REVIEW]' if r.stage == REVIEW_DIR else '[FAILED]'
        log(f"{tag} {album_base}: {r}")
        _move_album(album_dir, artist_base, r.stage)
        return

    if os.path.isdir(dest_dir):
        if mode == 'convert' and all(_output_ok(os.path.join(dest_dir, strip_mbid(os.path.splitext(os.path.basename(p))[0]) + '.m4a'), tags)
                                     for p, _, tags in files):
            log(f"[SKIP] {album_base}: already cleaned, removing source")
            shutil.rmtree(album_dir)
            _rmdir_if_empty(os.path.dirname(album_dir))
            return
        log(f"[SKIP] {album_base}: destination exists ({dest_dir}) — left in place")
        return

    os.makedirs(dest_dir, exist_ok=True)
    try:
        for p, audio, tags in files:
            if shutdown_requested:
                raise RuntimeError("shutdown requested")
            stem = strip_mbid(os.path.splitext(os.path.basename(p))[0])
            if mode == 'copy':
                dst = os.path.join(dest_dir, stem + os.path.splitext(p)[1].lower())
                timed(f"copy {os.path.basename(p)}", fast_copy, p, dst)
                continue
            dst = os.path.join(dest_dir, stem + '.m4a')
            src_md5 = pcm_md5(p)
            timed(f"alac {os.path.basename(p)}", convert_to_alac, p, dst, source_bits(audio))
            pic = source_picture(audio)
            write_mp4_tags(dst, dict(audio.tags), pic)
            verify_output(p, dst, dict(audio.tags), pic is not None, src_md5)
        produced = [e for e in os.listdir(dest_dir) if os.path.splitext(e)[1].lower() in ('.m4a', '.mp3', '.aac')]
        if len(produced) != len(files):
            raise RuntimeError(f"output count {len(produced)} != source {len(files)}")
    except Exception as e:
        log(f"❌ ERROR {album_base}: {e}")
        shutil.rmtree(dest_dir, ignore_errors=True)
        _rmdir_if_empty(os.path.dirname(dest_dir))
        _move_album(album_dir, artist_base, FAILED_DIR)
        return

    shutil.rmtree(album_dir)
    _rmdir_if_empty(os.path.dirname(album_dir))
    log(f"✔ [DONE] {out_artist}/{out_album} ({len(files)} tracks, {mode})")
    if OPTS.publish:
        publish_album(dest_dir)


def find_albums():
    albums = []
    if not os.path.isdir(SOURCE_DIR):
        return albums
    for artist in sorted(os.listdir(SOURCE_DIR)):
        a_dir = os.path.join(SOURCE_DIR, artist)
        if artist.startswith('.') or not os.path.isdir(a_dir):
            continue
        for album in sorted(os.listdir(a_dir)):
            al_dir = os.path.join(a_dir, album)
            if not album.startswith('.') and os.path.isdir(al_dir):
                albums.append(al_dir)
    return albums


def find_cleaned_albums():
    out = []
    if not os.path.isdir(DEST_DIR):
        return out
    for artist in sorted(os.listdir(DEST_DIR)):
        a = os.path.join(DEST_DIR, artist)
        if artist.startswith('.') or not os.path.isdir(a):
            continue
        for album in sorted(os.listdir(a)):
            d = os.path.join(a, album)
            if not album.startswith('.') and os.path.isdir(d):
                out.append(d)
    return out


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--no-publish', action='store_true', help='Stop at cleaned_music/.')
    ap.add_argument('--publish-only', action='store_true', help='Skip cleaning; publish what is in cleaned_music/.')
    ap.add_argument('--no-lidarr', action='store_true', help='Publish but do not touch Lidarr.')
    ap.add_argument('--library', help='Destination folder (default $RC_MEDIA_BASE/music).')
    ap.add_argument('--skip-perms', action='store_true', help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.no_publish and a.publish_only:
        ap.error('--no-publish and --publish-only are mutually exclusive')
    OPTS.publish = not a.no_publish
    OPTS.clean = not a.publish_only
    OPTS.lidarr = not a.no_lidarr
    OPTS.library = a.library
    OPTS.skip_perms = a.skip_perms


def main(argv=None):
    global shutdown_requested
    parse_args(sys.argv[1:] if argv is None else argv)

    for d in (LOG_DIR, DEST_DIR, REVIEW_DIR, FAILED_DIR):
        os.makedirs(d, exist_ok=True)
    if OPTS.publish and not os.path.isdir(library_dir()):
        log(f"❌ destination missing: {library_dir()}")
        return

    if not OPTS.clean:
        albums = find_cleaned_albums()
        log(f"▶ Publishing {len(albums)} albums from {DEST_DIR}")
        done = 0
        for d in albums:
            try:
                done += bool(publish_album(d))
            except Exception as e:
                log(f"❌ ERROR publishing {d}: {e}")
        log(f"All albums processed. {done}/{len(albums)} published.")
        return

    srcs = find_albums()
    log(f"▶ Queued {len(srcs)} albums for cleaning with up to {MAX_WORKERS} workers"
        + ("" if OPTS.publish else " (--no-publish)"))

    if srcs:
        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = [pool.submit(clean_album, src) for src in srcs]
                completed = 0
                try:
                    for fut in as_completed(futures):
                        completed += 1
                        log(f"📊 Progress: {completed}/{len(futures)} albums completed")

                        if shutdown_requested:
                            log("⚠️ Shutdown requested, cancelling remaining tasks...")
                            for f in futures:
                                f.cancel()
                            pool.shutdown(wait=False)
                            return

                        try:
                            fut.result(timeout=3600)
                        except Exception as e:
                            log(f"❌ Worker thread error: {e}")
                            log(f"❌ Worker thread traceback: {traceback.format_exc()}")
                except KeyboardInterrupt:
                    log("⚠️ Received interrupt signal, shutting down gracefully...")
                    shutdown_requested = True
                    for f in futures:
                        f.cancel()
                    pool.shutdown(wait=False)
                    return
        except KeyboardInterrupt:
            log("⚠️ Received interrupt signal during startup, exiting...")
            return

    log("All albums processed.")


if __name__ == "__main__":
    main()
