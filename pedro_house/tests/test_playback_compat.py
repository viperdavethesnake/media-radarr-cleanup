import importlib
import json
import shutil
import subprocess

import pytest

from pedro_house import playback_compat as pc


def _audio(tid, codec_id, codec, ch, lang='eng'):
    return {'id': tid, 'type': 'audio', 'codec': codec,
            'properties': {'codec_id': codec_id, 'audio_channels': ch, 'language': lang}}


def _probe(t):
    p = t.get('properties') or {}
    return f"{p.get('codec_id') or ''} {t.get('codec') or ''}"


TRUEHD_8 = _audio(1, 'A_TRUEHD', 'TrueHD Atmos', 8)
DTSHD_6 = _audio(2, 'A_DTS', 'DTS-HD Master Audio', 6)
AC3_6 = _audio(3, 'A_AC3', 'AC-3', 6)
AC3_2 = _audio(4, 'A_AC3', 'AC-3', 2)
EAC3_6 = _audio(5, 'A_EAC3', 'E-AC-3', 6)
AAC_2 = _audio(6, 'A_AAC', 'AAC', 2)
AAC_6 = _audio(7, 'A_AAC', 'AAC', 6)


def test_ac3_beats_lossless():
    assert pc.pick_compat_audio([TRUEHD_8, DTSHD_6, AC3_6], _probe) is AC3_6


def test_eac3_beats_lossless_when_no_ac3():
    assert pc.pick_compat_audio([TRUEHD_8, EAC3_6], _probe) is EAC3_6


def test_stereo_ac3_downmix_loses_to_surround_source():
    assert pc.pick_compat_audio([AC3_2, TRUEHD_8], _probe) is TRUEHD_8


def test_stereo_only_file_keeps_stereo_ac3():
    assert pc.pick_compat_audio([AAC_2, AC3_2], _probe) is AC3_2


def test_needs_ac3():
    assert pc.needs_ac3(TRUEHD_8, _probe(TRUEHD_8))
    assert pc.needs_ac3(DTSHD_6, _probe(DTSHD_6))
    assert pc.needs_ac3(AAC_6, _probe(AAC_6))      # would reach the AVR as stereo
    assert not pc.needs_ac3(AC3_6, _probe(AC3_6))
    assert not pc.needs_ac3(EAC3_6, _probe(EAC3_6))
    assert not pc.needs_ac3(AAC_2, _probe(AAC_2))  # TV plays stereo AAC


def _video(tid, dims, codec='HEVC/H.265/MPEG-H', codec_id='V_MPEGH/ISO/HEVC'):
    return {'id': tid, 'type': 'video', 'codec': codec,
            'properties': {'codec_id': codec_id, 'pixel_dimensions': dims}}


def test_dv7_dual_track_keeps_base_layer():
    bl, el = _video(0, '3840x2160'), _video(1, '1920x1080')
    assert pc.split_dv7_layers([bl, el], {0: 7}, _probe) == (bl, el)
    assert pc.split_dv7_layers([el, bl], {0: 7}, _probe) == (bl, el)


def test_two_video_tracks_without_dv7_still_go_to_review():
    a, b = _video(0, '3840x2160'), _video(1, '1920x1080')
    assert pc.split_dv7_layers([a, b], {}, _probe) is None
    assert pc.split_dv7_layers([a, _video(1, '3840x2160')], {0: 7}, _probe) is None
    avc = _video(1, '1920x1080', 'AVC/H.264/MPEG-4p10', 'V_MPEG4/ISO/AVC')
    assert pc.split_dv7_layers([a, avc], {0: 7}, _probe) is None


# --- end to end: real ffmpeg + mkvmerge on a tiny synthetic file ------------

needs_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ('ffmpeg', 'ffprobe', 'mkvmerge')),
    reason='ffmpeg/ffprobe/mkvmerge not installed')


def _make_mkv(path, audio_args):
    subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=s=320x240:r=24:d=5',
                    '-f', 'lavfi', '-i', 'sine=f=440:d=5', *audio_args,
                    '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-metadata:s:a:0', 'language=eng', str(path)], check=True)


def _streams(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                          '-of', 'json', str(path)], capture_output=True, text=True,
                         check=True).stdout
    return json.loads(out)


@pytest.fixture
def movie_remux(tmp_path, monkeypatch):
    monkeypatch.setenv('RC_SERVARR_DIR', str(tmp_path))
    from movie_cleanup import mkv_remux_cleanroom as m
    importlib.reload(m)
    for d in ('logs', 'tagged', 'cleaned', 'review', 'failed'):
        (tmp_path / d).mkdir()
    return m, tmp_path


@needs_tools
@pytest.mark.parametrize('compat', [True, False])
def test_flac_51_becomes_ac3_only_with_compat(movie_remux, monkeypatch, compat):
    m, base = movie_remux
    monkeypatch.setattr(m, 'COMPAT', compat)
    folder = base / 'tagged' / 'Film_(2020)'
    folder.mkdir()
    _make_mkv(folder / 'Film.mkv', ['-af', 'pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0',
                                    '-c:a', 'flac'])
    m.remux_folder(str(folder))

    outs = list((base / 'cleaned' / 'Film_(2020)').glob('*'))
    assert [o.name for o in outs] == ['Film.mkv']        # temp audio file removed
    info = _streams(outs[0])
    audio = [s for s in info['streams'] if s['codec_type'] == 'audio']
    assert len(audio) == 1
    assert audio[0]['codec_name'] == ('ac3' if compat else 'flac')
    assert audio[0]['channels'] == 6
    assert audio[0]['tags'].get('language') == 'eng'
    assert info['streams'][0]['codec_type'] == 'video'   # video stays first
    assert abs(float(info['format']['duration']) - 5.0) < 0.2


def test_extra_inputs_orders_converted_tracks():
    assert pc.extra_inputs('0', '1') == []
    assert pc.extra_inputs('0', '1', ac3_file='a.mka') == \
        ['--language', '0:eng', 'a.mka', '--track-order', '0:0,1:0']
    assert pc.extra_inputs('0', '2', hdr10_file='v.mkv') == \
        ['v.mkv', '--track-order', '1:0,0:2']
    assert pc.extra_inputs('0', '2', 'v.mkv', 'a.mka', 'fre') == \
        ['v.mkv', '--language', '0:fre', 'a.mka', '--track-order', '1:0,2:0']
