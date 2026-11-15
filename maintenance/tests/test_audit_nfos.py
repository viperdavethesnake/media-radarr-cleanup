"""audit_nfos.py: accepts both NFO writers' output, flags real defects."""
import os
import sys

import pytest

from maintenance import audit_nfos as m

GOOD = """<?xml version="1.0" encoding="utf-8"?>
<movie>
  <title>A Beautiful Mind</title>
  <sorttitle>Beautiful Mind, A</sorttitle>
  <year>2001</year>
  <premiered>2001-12-11</premiered>
  <plot>Plot &amp; more.</plot>
  <runtime>135</runtime>
  <genre>Drama</genre>
  <tmdbid>453</tmdbid>
  <imdbid>tt0268978</imdbid>
  <uniqueid type="tmdb" default="true">453</uniqueid>
  <uniqueid type="imdb">tt0268978</uniqueid>
  {extra}
</movie>"""


def make(lib, name, extra='', body=None, files=('poster.jpg', 'fanart.jpg')):
    d = lib / name
    d.mkdir(parents=True)
    (d / 'movie.nfo').write_text(body if body is not None else GOOD.format(extra=extra))
    (d / f'{name}_[1080p_h264_ac3].mkv').write_bytes(b'')
    for f in files:
        (d / f).write_bytes(b'')
    return d


def codes(r, worst='WARN'):
    return {i['code'] for i in r['issues'] if m.SEV_ORDER[i['sev']] <= m.SEV_ORDER[worst]}


def test_batch_cleaner_style_nfo_is_clean(tmp_path):
    extra = '<set><name>X Collection</name></set><setid>99</setid>'
    assert codes(m.audit(str(make(tmp_path, 'A_Beautiful_Mind_(2001)', extra)))) == set()


def test_regenerate_nfos_style_collection_is_clean(tmp_path):
    extra = ('<set><name>X Collection</name></set><collectionnumber>99</collectionnumber>'
             '<uniqueid type="tmdbcol">99</uniqueid>')
    assert codes(m.audit(str(make(tmp_path, 'A_Beautiful_Mind_(2001)', extra)))) == set()


@pytest.mark.parametrize('title,stitle,ok', [
    ('A Beautiful Mind', 'Beautiful Mind, A', True),
    ('The Matrix', 'Matrix', True),
    ('Heat', 'Heat', True),
    ('The Matrix', 'Speed', False),
])
def test_sorttitle(title, stitle, ok):
    assert m.sorttitle_ok(title, stitle) is ok


def test_flags_double_escape_and_id_mismatch(tmp_path):
    body = GOOD.format(extra='').replace('Plot &amp; more', 'Plot &amp;amp; more') \
                                .replace('<tmdbid>453', '<tmdbid>454')
    c = codes(m.audit(str(make(tmp_path, 'A_Beautiful_Mind_(2001)', body=body))))
    assert {'double_escaped_entity', 'tmdbid_vs_uniqueid'} <= c


def test_collection_ids_disagree(tmp_path):
    extra = '<set><name>X</name></set><setid>1</setid><collectionnumber>2</collectionnumber>'
    assert 'collection_ids_disagree' in codes(m.audit(str(make(tmp_path, 'A_Beautiful_Mind_(2001)', extra))))


def test_folder_and_mkv_names(tmp_path):
    d = make(tmp_path, 'A_Beautiful_Mind_(2002)')
    c = codes(m.audit(str(d)))
    assert 'folder_year_vs_nfo' in c


def test_bad_xml_and_missing_nfo_do_not_crash(tmp_path):
    bad = m.audit(str(make(tmp_path, 'Bad_(2001)', body='<movie><title>x</movie>')))
    assert codes(bad) == {'xml_parse_error'}
    empty = tmp_path / 'Empty_(2001)'
    empty.mkdir()
    assert codes(m.audit(str(empty))) == {'no_nfo'}


def test_duplicate_ids_same_vs_cross_library(tmp_path):
    a, b = tmp_path / 'movies', tmp_path / 'movies2'
    r = [m.audit(str(make(a, 'A_Beautiful_Mind_(2001)'))),
         m.audit(str(make(b, 'A_Beautiful_Mind_(2001)')))]
    m.cross_check(r)
    assert 'duplicate_tmdbid_cross_lib' in codes(r[0])
    r2 = [m.audit(str(make(a, 'Copy_(2001)', body=GOOD.format(extra='')))), r[0]]
    for x in r2:
        x['issues'] = [i for i in x['issues'] if not i['code'].startswith('duplicate_')]
    m.cross_check(r2)
    assert 'duplicate_tmdbid_same_lib' in codes(r2[0])


def test_main_skips_symlinks_and_writes_exports(tmp_path, monkeypatch, capsys):
    lib = tmp_path / 'lib'
    real = make(lib, 'A_Beautiful_Mind_(2001)')
    os.symlink(real, lib / 'Linked_(2001)')
    j, c = tmp_path / 'o.json', tmp_path / 'o.csv'
    monkeypatch.setattr(sys, 'argv', ['audit_nfos.py', str(lib), '--json', str(j), '--csv', str(c)])
    m.main()
    assert '1 folders scanned' in capsys.readouterr().out
    assert j.exists() and c.read_text().startswith('severity,code,folder,detail')
