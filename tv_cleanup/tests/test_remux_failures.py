"""A failed episode stays in tagged_tv/ for retry, is named in the end-of-run summary,
and nothing is ever written to failed_tv/."""
import importlib
import os

import pytest


@pytest.fixture
def remux(tmp_path, monkeypatch):
    monkeypatch.setenv('RC_SERVARR_DIR', str(tmp_path))
    from tv_cleanup import tv_mkv_remux_cleanroom as m
    importlib.reload(m)
    show = tmp_path / 'tagged_tv' / 'Show_(2020)' / 'Season 01'
    show.mkdir(parents=True)
    (show / 'Show_S01E01_Pilot.mkv').write_bytes(b'')
    (show / 'Show_S01E02_Two.mkv').write_bytes(b'')
    return m, tmp_path


def test_failed_episode_is_summarised_and_left_in_place(remux, monkeypatch):
    m, base = remux

    def fake(job):
        if 'E02' in job['src_mkv']:
            return ('failed', job, 'mkvmerge exploded')
        return ('ok', job, None)

    monkeypatch.setattr(m, 'remux_episode', fake)
    m.main()
    log = (base / 'logs' / 'tv_remux_cleanroom_debug.log').read_text()
    assert '1 episode(s) failed' in log
    assert 'Show_S01E02_Two.mkv' in log and 'mkvmerge exploded' in log
    assert (base / 'tagged_tv' / 'Show_(2020)' / 'Season 01' / 'Show_S01E02_Two.mkv').exists()
    assert not (base / 'failed_tv').exists()
    assert not hasattr(m, '_move_to_failed')
