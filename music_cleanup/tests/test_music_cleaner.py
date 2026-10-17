import importlib
import os
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from .conftest import make_album, pcm_md5

from music_cleanup import music_cleaner as mc

FF = lambda k: f'----:com.apple.iTunes:{k}'


def run(*args):
    mc._lidarr = None
    mc.main(list(args))


def clean_only():
    run('--no-publish')


def test_converts_album_bit_exact_with_tags(stage):
    al_dir, files, rg, am = make_album(stage)
    src_tags = {p.name: dict(FLAC(str(p)).tags) for p in files}
    src_md5 = {p.name: pcm_md5(p) for p in files}
    clean_only()
    out_dir = stage / 'cleaned_music' / 'Test_Artist' / 'Test_Album_(2026)'
    outs = sorted(out_dir.glob('*.m4a'))
    assert len(outs) == 3
    assert not al_dir.exists() and not al_dir.parent.exists()
    for o in outs:
        src_name = o.name[:-4] + '.flac'
        assert pcm_md5(o) == src_md5[src_name]
        t = MP4(str(o)).tags
        s = src_tags[src_name]
        assert t['\xa9nam'] == s['title']
        assert t['\xa9alb'] == s['album']
        assert t['aART'] == s['albumartist']
        assert t['\xa9ART'] == s['artist']
        assert t['\xa9day'] == s['date']
        assert t['trkn'] == [(int(s['tracknumber'][0]), int(s['tracktotal'][0]))]
        assert t['disk'] == [(1, 1)]
        for vk, ak in (('musicbrainz_releasegroupid', 'MusicBrainz Release Group Id'),
                       ('musicbrainz_albumid', 'MusicBrainz Album Id'),
                       ('musicbrainz_artistid', 'MusicBrainz Artist Id'),
                       ('musicbrainz_albumartistid', 'MusicBrainz Album Artist Id'),
                       ('musicbrainz_trackid', 'MusicBrainz Track Id'),
                       ('musicbrainz_releasetrackid', 'MusicBrainz Release Track Id'),
                       ('label', 'LABEL'), ('media', 'MEDIA'),
                       ('releasetype', 'MusicBrainz Album Type'),
                       ('releasestatus', 'MusicBrainz Album Status'),
                       ('releasecountry', 'MusicBrainz Album Release Country'),
                       ('originaldate', 'RELEASEDATE')):
            assert [b.decode() for b in t[FF(ak)]] == s[vk], ak
        assert len(t['covr']) == 1
    assert (stage / 'logs' / 'music_cleaner_debug.log').exists()


def test_missing_releasegroup_goes_to_review(stage):
    al_dir, files, rg, am = make_album(stage, extra_for=2, extra={'musicbrainz_releasegroupid': None})
    clean_only()
    assert (stage / 'review_music' / al_dir.parent.name / al_dir.name).is_dir()
    assert not (stage / 'cleaned_music' / 'Test_Artist').exists()
    assert not al_dir.exists()


def test_tracktotal_mismatch_goes_to_review(stage):
    al_dir, *_ = make_album(stage, total=4)
    clean_only()
    assert (stage / 'review_music' / al_dir.parent.name / al_dir.name).is_dir()


def test_ogg_goes_to_failed(stage):
    al_dir, *_ = make_album(stage, ext='ogg')
    clean_only()
    assert (stage / 'failed_music' / al_dir.parent.name / al_dir.name).is_dir()
    assert not (stage / 'cleaned_music' / 'Test_Artist').exists()


def test_missing_mbid_token_goes_to_failed(stage):
    al_dir, *_ = make_album(stage)
    bad = al_dir.parent / 'Test_Album_(2026)'
    al_dir.rename(bad)
    clean_only()
    assert (stage / 'failed_music' / bad.parent.name / bad.name).is_dir()


def test_rerun_skips_existing_output(stage):
    make_album(stage)
    clean_only()
    out_dir = stage / 'cleaned_music' / 'Test_Artist' / 'Test_Album_(2026)'
    before = sorted(p.name for p in out_dir.iterdir())
    make_album(stage)   # same names, new mbids -> destination exists
    clean_only()
    assert sorted(p.name for p in out_dir.iterdir()) == before
    assert any(p.is_dir() for p in (stage / 'music').iterdir())   # left in place
    log = (stage / 'logs' / 'music_cleaner_debug.log').read_text()
    assert '[SKIP]' in log


def test_date_falls_back_to_originaldate(stage):
    make_album(stage, n=1, bits=(16,), extra_for=1, extra={'date': None, 'originaldate': '2001-02-03'})
    clean_only()
    out = next((stage / 'cleaned_music' / 'Test_Artist' / 'Test_Album_(2026)').glob('*.m4a'))
    assert MP4(str(out)).tags['\xa9day'] == ['2001-02-03']


# ---------- end to end ----------

def test_default_run_publishes_and_hands_off_to_lidarr(stage, library):
    media, wire = library
    al_dir, files, rg, am = make_album(stage)
    fake = wire(rg)
    run('--skip-perms')
    target = media / 'music' / 'Test_Artist' / 'Test_Album_(2026)'
    assert len(list(target.glob('*.m4a'))) == 3
    assert not (stage / 'cleaned_music' / 'Test_Artist').exists()
    assert not al_dir.exists()
    assert oct(os.stat(target.parent).st_mode & 0o7777) == '0o2775'
    assert (stage / 'music' / '.lidarr_keep').is_dir()
    put = next(b for m, p, b in fake.calls if m == 'PUT')
    assert put['monitored'] is False and p_startswith(fake, 'PUT', '/api/v1/album/17')
    dele = next(b for m, p, b in fake.calls if m == 'DELETE')
    assert dele == {'trackFileIds': [1, 2]}


def p_startswith(fake, method, prefix):
    return any(m == method and p.startswith(prefix) for m, p, _ in fake.calls)


def test_no_publish_stops_at_cleaned(stage, library):
    media, wire = library
    _, _, rg, _ = make_album(stage)
    fake = wire(rg)
    run('--no-publish', '--skip-perms')
    assert (stage / 'cleaned_music' / 'Test_Artist' / 'Test_Album_(2026)').is_dir()
    assert not (media / 'music' / 'Test_Artist').exists()
    assert fake.calls == []


def test_publish_only_publishes_existing_cleaned(stage, library):
    media, wire = library
    _, _, rg, _ = make_album(stage)
    fake = wire(rg)
    run('--no-publish', '--skip-perms')
    make_album(stage, album='Other_(2026)')      # new staging album must NOT be cleaned
    run('--publish-only', '--skip-perms')
    assert (media / 'music' / 'Test_Artist' / 'Test_Album_(2026)').is_dir()
    assert any((stage / 'music').glob('*/Other_(2026)_*'))
    assert p_startswith(fake, 'DELETE', '/api/v1/trackfile/bulk')


def test_no_lidarr_publishes_without_api_calls(stage, library):
    media, wire = library
    _, _, rg, _ = make_album(stage)
    fake = wire(rg)
    run('--no-lidarr', '--skip-perms')
    assert (media / 'music' / 'Test_Artist' / 'Test_Album_(2026)').is_dir()
    assert fake.calls == []


def test_existing_target_is_not_overwritten(stage, library):
    media, wire = library
    _, _, rg, _ = make_album(stage)
    fake = wire(rg)
    existing = media / 'music' / 'Test_Artist' / 'Test_Album_(2026)'
    existing.mkdir(parents=True); (existing / 'keep.txt').write_text('x')
    run('--skip-perms')
    assert (existing / 'keep.txt').exists()
    assert (stage / 'cleaned_music' / 'Test_Artist' / 'Test_Album_(2026)').is_dir()
    assert fake.calls == []
    assert '[SKIP]' in (stage / 'logs' / 'music_cleaner_debug.log').read_text()


def test_missing_lidarr_env_warns_but_publishes(stage, library, monkeypatch):
    media, wire = library
    make_album(stage)
    monkeypatch.setenv('RC_SITE', '33')
    # Empty beats .env: load_dotenv never overrides a variable already set.
    monkeypatch.setenv('LIDARR_URL_33', ''); monkeypatch.setenv('LIDARR_API_KEY_33', '')
    run('--skip-perms')
    assert (media / 'music' / 'Test_Artist' / 'Test_Album_(2026)').is_dir()
    assert '[WARN]' in (stage / 'logs' / 'music_cleaner_debug.log').read_text()


def test_lidarr_block_follows_site(stage, library, monkeypatch):
    media, wire = library
    make_album(stage)
    fake = wire('11111111-1111-1111-1111-111111111111')
    monkeypatch.setenv('RC_SITE', '36')          # only the _33 block is wired
    monkeypatch.setenv('LIDARR_URL_36', ''); monkeypatch.setenv('LIDARR_API_KEY_36', '')
    run('--skip-perms')
    assert fake.calls == []
    assert 'LIDARR_URL_36' in (stage / 'logs' / 'music_cleaner_debug.log').read_text()


def test_pick_site_from_local_ips():
    assert mc.pick_site(['127.0.0.1', '192.168.33.40'], None) == '33'
    assert mc.pick_site(['192.168.36.40'], None) == '36'
    assert mc.pick_site(['10.0.0.5'], '33') == '33'
    assert mc.pick_site(['10.0.0.5'], None) is None
    assert mc.pick_site(['192.168.33.1', '192.168.36.1'], None) is None
    assert mc.pick_site(['192.168.33.1'], '99') is None


def test_servarr_dir_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv('RC_SERVARR_DIR', str(tmp_path))
    importlib.reload(mc)
    assert mc.SERVARR_DIR == str(tmp_path)
    assert mc.SOURCE_DIR == os.path.join(str(tmp_path), 'music')
    assert mc.LOG_DIR == os.path.join(str(tmp_path), 'logs')
    assert mc.LIDARR_KEEP_DIR == os.path.join(str(tmp_path), 'music', '.lidarr_keep')


def test_servarr_dir_falls_back_to_media_base(tmp_path, monkeypatch):
    monkeypatch.setenv('RC_SERVARR_DIR', '')
    monkeypatch.setenv('RC_MEDIA_BASE', str(tmp_path))
    importlib.reload(mc)
    assert mc.SERVARR_DIR == os.path.join(str(tmp_path), 'servarr')
    assert mc.DEST_DIR == os.path.join(str(tmp_path), 'servarr', 'cleaned_music')
