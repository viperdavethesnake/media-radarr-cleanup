"""Spec entries using the pre-`subtitles` keys must be rejected, not silently ignored."""
import json
import os
import runpy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def rf():
    os.environ.setdefault('TMDB_API_KEY', 'x')
    return runpy.run_path(str(REPO / 'foreign_cleanup' / 'remux_foreign.py'), run_name='not_main')


INFO = {'tracks': [
    {'id': 0, 'type': 'video', 'properties': {}},
    {'id': 1, 'type': 'audio', 'properties': {'language': 'chi', 'codec_id': 'A_TRUEHD'}},
]}


def test_obsolete_subtitle_keys_are_a_validation_problem(rf):
    spec = {'folder': 'F', 'src_folder': '/nowhere', 'video': 0, 'audio_default': 1,
            'subtitle_srt': '/tmp/x.srt', 'sub_lang': 'eng', 'sub_name': 'English'}
    problems = rf['validate'](spec, INFO)
    assert any('subtitle_srt' in p for p in problems)


def test_current_schema_passes(rf):
    spec = {'folder': 'F', 'src_folder': '/nowhere', 'video': 0, 'audio_default': 1, 'subtitles': []}
    assert rf['validate'](spec, INFO) == []


def test_no_committed_spec_uses_obsolete_keys():
    for p in sorted((REPO / 'foreign_cleanup').glob('spec_*.json')):
        for entry in json.load(open(p)):
            bad = {'subtitle_srt', 'sub_lang', 'sub_name'} & set(entry)
            assert not bad, f'{p.name}: {entry["folder"]} still uses {sorted(bad)}'
