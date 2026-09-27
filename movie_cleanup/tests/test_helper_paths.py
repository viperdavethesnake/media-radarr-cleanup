import runpy
from pathlib import Path

import pytest

HELPERS = Path(__file__).resolve().parents[1] / 'helpers'


@pytest.mark.parametrize('script', ['audit_nfo_titles', 'fix_nfo_titles', 'spot_check_movies'])
def test_helper_movies_dir_derives_from_media_base(script, monkeypatch):
    monkeypatch.setenv('RC_MEDIA_BASE', '/x')
    m = runpy.run_path(str(HELPERS / f'{script}.py'), run_name='not_main')
    assert m['MOVIES_DIR'] == '/x/movies'
