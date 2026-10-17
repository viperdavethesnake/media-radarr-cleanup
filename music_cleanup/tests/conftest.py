import importlib, os, subprocess, uuid, sys
import pytest
from mutagen.flac import FLAC, Picture

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

PNG_1x1 = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478'
    '9c63f8cfc0f01f0005000101f8b0d0f70000000049454e44ae426082')

BASE_TAGS = {
    'albumartist': 'Test Artist', 'artist': 'Test Artist', 'album': 'Test Album',
    'date': '2026-01-15', 'originaldate': '2026-01-15', 'genre': 'Test',
    'label': 'Test Label', 'media': 'Digital Media', 'releasetype': 'album',
    'releasestatus': 'Official', 'releasecountry': 'US',
    'musicbrainz_albumid': str(uuid.uuid4()),
    'musicbrainz_artistid': str(uuid.uuid4()),
    'musicbrainz_albumartistid': str(uuid.uuid4()),
}


def make_flac(path, seconds=1.0, bits=16, rate=44100, freq=440):
    fmt = 's16' if bits == 16 else 's32'
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
           '-f', 'lavfi', '-i', f'sine=frequency={freq}:duration={seconds}',
           '-ar', str(rate), '-ac', '2', '-sample_fmt', fmt, '-c:a', 'flac']
    if bits == 24:
        cmd += ['-bits_per_raw_sample', '24']
    subprocess.run(cmd + [path], check=True)


def tag_flac(path, n, total, rg_mbid, extra=None, cover=True):
    f = FLAC(path)
    f.delete()
    for k, v in BASE_TAGS.items():
        f[k] = v
    f['title'] = f'Track {n}: Song / Part {n}'
    f['tracknumber'] = str(n); f['tracktotal'] = str(total)
    f['discnumber'] = '1'; f['disctotal'] = '1'
    f['musicbrainz_releasegroupid'] = rg_mbid
    f['musicbrainz_trackid'] = str(uuid.uuid4())
    f['musicbrainz_releasetrackid'] = str(uuid.uuid4())
    if extra:
        for k, v in extra.items():
            if v is None:
                del f[k]
            else:
                f[k] = v
    if cover:
        p = Picture(); p.type = 3; p.mime = 'image/png'; p.data = PNG_1x1
        f.add_picture(p)
    f.save()


@pytest.fixture(autouse=True)
def isolated_servarr(tmp_path, monkeypatch):
    """Every test gets its own RC_SERVARR_DIR so none can touch the real servarr tree."""
    monkeypatch.setenv('RC_SERVARR_DIR', str(tmp_path))
    from music_cleanup import music_cleaner
    importlib.reload(music_cleaner)
    return tmp_path


@pytest.fixture
def stage(isolated_servarr):
    """Scratch servarr tree laid out with music/ and logs/."""
    for d in ('music', 'logs'):
        (isolated_servarr / d).mkdir()
    return isolated_servarr


def make_album(stage, artist='Test Artist', album='Test_Album_(2026)', n=3, bits=(16, 16, 24),
               total=None, rg_mbid=None, artist_mbid=None, extra_for=None, extra=None, ext='flac'):
    rg_mbid = rg_mbid or str(uuid.uuid4())
    artist_mbid = artist_mbid or str(uuid.uuid4())
    a_dir = stage / 'music' / f"{artist.replace(' ', '_')}_[mbid-{artist_mbid}]"
    al_dir = a_dir / f"{album}_[mbid-{rg_mbid}]"
    al_dir.mkdir(parents=True)
    files = []
    for i in range(1, n + 1):
        b = bits[(i - 1) % len(bits)]
        rate = 96000 if b == 24 else 44100
        p = al_dir / f"{artist.replace(' ', '_')}_-_{album}_-_{i:02d}_-_Track_{i}.{ext}"
        if ext == 'flac':
            make_flac(str(p), bits=b, rate=rate, freq=300 + 50 * i)
            tag_flac(str(p), i, total or n, rg_mbid, extra=(extra if extra_for == i else None))
        else:
            subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                            '-i', 'sine=frequency=440:duration=1', '-ac', '2', str(p)], check=True)
        files.append(p)
    return al_dir, files, rg_mbid, artist_mbid


def pcm_md5(path):
    out = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(path),
                          '-map', '0:a:0', '-vn', '-af', 'aformat=sample_fmts=s32', '-f', 'md5', '-'],
                         check=True, capture_output=True, text=True).stdout.strip()
    return out


class FakeLidarr:
    """Minimal Lidarr v1 stand-in: one album keyed by release-group id; records every call."""

    def __init__(self, rg_mbid, monitored=True, trackfile_ids=(1, 2)):
        import json, threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        self.calls = []
        album = {'id': 17, 'title': 'Test Album', 'monitored': monitored, 'artistId': 1, 'foreignAlbumId': rg_mbid}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _send(self, code, body):
                b = json.dumps(body).encode()
                self.send_response(code); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            def _body(self):
                n = int(self.headers.get('Content-Length') or 0)
                return json.loads(self.rfile.read(n)) if n else None
            def do_GET(self):
                fake.calls.append(('GET', self.path, None))
                if self.path.startswith('/api/v1/album?foreignAlbumId=' + rg_mbid):
                    return self._send(200, [album])
                if self.path.startswith('/api/v1/trackfile?albumId=17'):
                    return self._send(200, [{'id': i} for i in trackfile_ids])
                return self._send(200, [])
            def do_PUT(self):
                b = self._body(); fake.calls.append(('PUT', self.path, b)); album.update(b); self._send(202, album)
            def do_DELETE(self):
                b = self._body(); fake.calls.append(('DELETE', self.path, b)); self._send(200, {})

        self.server = HTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def library(stage, monkeypatch):
    """A scratch $RC_MEDIA_BASE with music/ inside, and a fake Lidarr wired via env."""
    media = stage / 'media'
    (media / 'music').mkdir(parents=True)
    monkeypatch.setenv('RC_MEDIA_BASE', str(media))
    holder = {}

    def wire(rg_mbid, **kw):
        fake = FakeLidarr(rg_mbid, **kw)
        monkeypatch.setenv('RC_SITE', '33')
        monkeypatch.setenv('LIDARR_URL_33', fake.url)
        monkeypatch.setenv('LIDARR_API_KEY_33', 'k')
        holder['fake'] = fake
        return fake

    yield media, wire
    if 'fake' in holder:
        holder['fake'].stop()
