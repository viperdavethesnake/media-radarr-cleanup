import importlib
import os


def test_log_creates_missing_servarr_tree(tmp_path, monkeypatch):
    servarr = tmp_path / 'nested' / 'servarr'
    monkeypatch.setenv('RC_SERVARR_DIR', str(servarr))
    from movie_cleanup import verify_movies as vm
    importlib.reload(vm)
    vm.log('probe')
    assert (servarr / 'logs' / 'verify_movies_debug.log').exists()
