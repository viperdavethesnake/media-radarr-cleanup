"""radarr_upgrade_push.py picks the Radarr instance for the site it runs on."""
import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'radarr_upgrade_push.py'


def load(monkeypatch, env):
    for k in ('RC_SITE', 'RADARR_URL_33', 'RADARR_URL_36',
              'RADARR_API_KEY_33', 'RADARR_API_KEY_36'):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return runpy.run_path(str(SCRIPT), run_name='not_main')


def test_rc_site_override_selects_block(monkeypatch):
    m = load(monkeypatch, {'RC_SITE': '36', 'RADARR_API_KEY_36': 'k36',
                           'RADARR_API_KEY_33': 'k33'})
    assert m['SITE'] == '36'
    assert m['RADARR_URL'] == 'http://192.168.36.195:7878'
    assert m['API_KEY'] == 'k36'


def test_pick_site_from_local_ips(monkeypatch):
    m = load(monkeypatch, {'RC_SITE': '33'})
    pick = m['pick_site']
    assert pick(['127.0.0.1', '192.168.33.40'], None) == '33'
    assert pick(['192.168.36.40', '172.17.0.1'], None) == '36'
    assert pick(['10.0.0.5'], '33') == '33'


@pytest.mark.parametrize('ips,override', [
    (['10.0.0.5'], None),                      # no known site
    (['192.168.33.1', '192.168.36.1'], None),  # ambiguous
    (['192.168.33.1'], '99'),                  # bad override
])
def test_pick_site_rejects(ips, override, monkeypatch):
    m = load(monkeypatch, {'RC_SITE': '33'})
    with pytest.raises(SystemExit):
        m['pick_site'](ips, override)


def test_missing_key_fails_in_main_not_import(monkeypatch):
    # An empty value in the environment beats .env (load_dotenv never overrides).
    m = load(monkeypatch, {'RC_SITE': '33', 'RADARR_API_KEY_33': ''})
    assert m['API_KEY'] == ''
    monkeypatch.setattr('sys.argv', ['x'])
    with pytest.raises(SystemExit):
        m['main']()
