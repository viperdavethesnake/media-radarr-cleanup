"""Ranking rules for compare_movie_copies.recommend_keep and the probe parsers feeding it."""
import importlib

m = importlib.import_module('maintenance.compare_movie_copies')

NEW, EXISTING = m.SIDE_NEW, m.SIDE_EXISTING


def track(codec='eac3', profile='', lang='eng', channels='6ch(5.1(side))', atmos=False, bitrate_bps=640_000, idx=0):
    return m.TrackSummary(
        idx=idx, lang=lang, codec=codec, codec_long='', profile=profile, channels=channels,
        sample_rate_hz=48000, bitrate_bps=bitrate_bps, title='', default=True, atmos=atmos,
    )


def ms(res='3840x2160', codec='hevc', hdr='HDR10', vbr=20_000_000, dur=6000.0, size=None,
       tracks=None, bit_depth=10, dv_profile=''):
    tracks = tuple(tracks) if tracks is not None else (track(),)
    return m.MediaSummary(
        mkv_path='/x/movie.mkv', size_bytes=size, duration_s=dur,
        v_codec=codec, v_profile='', v_level='', v_res=res, v_fps=23.976,
        v_bit_depth=bit_depth, v_pix_fmt='', v_bitrate_bps=vbr, v_hdr_format=hdr,
        v_dv_profile=dv_profile, v_color_primaries='', v_color_transfer='', v_colorspace='',
        v_mastering='', v_maxcll=None, v_maxfall=None,
        audio_tracks=tracks, best_audio_idx=tracks[0].idx if tracks else None,
    )


def rec(new, existing):
    keep, basis, reasons, conf = m.recommend_keep(new, existing)
    return keep, basis, conf


# ── comparability gate ───────────────────────────────────────────────────────

def test_runtime_gap_over_two_minutes_goes_to_review():
    assert rec(ms(dur=6000), ms(dur=6000 + 121))[0] == 'REVIEW'


def test_runtime_gap_under_two_minutes_is_compared():
    assert rec(ms(dur=6000), ms(dur=6000 + 90))[0] != 'REVIEW'


def test_copy_without_english_audio_goes_to_review():
    assert rec(ms(tracks=[track(lang='fre')]), ms())[0] == 'REVIEW'


# ── video rungs ──────────────────────────────────────────────────────────────

def test_cropped_hdr_beats_uncropped_sdr_at_same_tier():
    # The End of Oak Street: 3840x1600 HDR10+ vs 3840x2160 SDR.
    keep, basis, conf = rec(ms(res='3840x1600', hdr='HDR10+'), ms(res='3840x2160', hdr='SDR', bit_depth=8))
    assert (keep, basis, conf) == (NEW, 'hdr', 'high')


def test_higher_resolution_tier_wins():
    assert rec(ms(res='1920x1080'), ms(res='3840x1600'))[:2] == (EXISTING, 'resolution')


def test_cropping_alone_is_a_tie():
    assert rec(ms(res='3840x1600'), ms(res='3840x2160'))[0] == 'TIE'


def test_dolby_vision_beats_hdr10():
    assert rec(ms(hdr='DV', dv_profile='8.1'), ms(hdr='HDR10'))[:2] == (NEW, 'hdr')


def test_remux_beats_encode_regardless_of_codec():
    keep, basis, conf = rec(ms(codec='av1', vbr=12_000_000), ms(codec='hevc', vbr=58_000_000))
    assert (keep, basis, conf) == (EXISTING, 'source', 'high')


def test_codec_alone_never_decides():
    assert rec(ms(codec='av1', vbr=20_000_000), ms(codec='hevc', vbr=30_000_000))[0] != NEW


def test_bitrate_is_adjusted_for_codec():
    # hevc 8 Mb/s ~ h264 12 Mb/s; vs h264 6 Mb/s that's a 2x gap.
    keep, basis, conf = rec(ms(res='1920x1080', codec='hevc', vbr=8_000_000),
                            ms(res='1920x1080', codec='h264', vbr=6_000_000))
    assert (keep, basis, conf) == (NEW, 'bitrate', 'high')


def test_bitrate_gap_under_25_percent_is_a_tie():
    assert rec(ms(vbr=20_000_000), ms(vbr=24_000_000))[0] == 'TIE'


def test_bitrate_gap_between_25_and_50_percent_is_medium():
    assert rec(ms(vbr=20_000_000), ms(vbr=27_000_000)) == (EXISTING, 'bitrate', 'medium')


# ── audio ────────────────────────────────────────────────────────────────────

def test_lossless_audio_overrides_small_bitrate_win():
    lossless = [track(codec='truehd', profile='Dolby TrueHD + Dolby Atmos', atmos=True)]
    assert rec(ms(vbr=20_000_000, tracks=lossless), ms(vbr=27_000_000)) == (NEW, 'audio', 'medium')


def test_lossless_audio_does_not_override_large_bitrate_win():
    lossless = [track(codec='truehd')]
    assert rec(ms(vbr=10_000_000, tracks=lossless), ms(vbr=27_000_000))[:2] == (EXISTING, 'bitrate')


def test_lossless_audio_never_overrides_hdr():
    lossless = [track(codec='truehd')]
    assert rec(ms(hdr='SDR', tracks=lossless), ms(hdr='HDR10'))[:2] == (EXISTING, 'hdr')


def test_video_tie_dts_hd_ma_beats_lossy():
    keep, basis, conf = rec(ms(tracks=[track(codec='dts', profile='DTS-HD MA')]), ms())
    assert (keep, basis, conf) == (NEW, 'audio', 'medium')


def test_video_tie_atmos_beats_plain():
    assert rec(ms(), ms(tracks=[track(atmos=True)]))[:2] == (EXISTING, 'audio')


def test_video_tie_more_channels_wins():
    assert rec(ms(tracks=[track(channels='2ch(stereo)')]), ms())[:2] == (EXISTING, 'audio')


def test_identical_copies_tie():
    assert rec(ms(), ms())[0] == 'TIE'


# ── probe parsers ────────────────────────────────────────────────────────────

def test_dts_hd_ma_profile_is_lossless_and_plain_dts_is_not():
    assert m._is_lossless(track(codec='dts', profile='DTS-HD MA'))
    assert not m._is_lossless(track(codec='dts', profile='DTS'))


def test_stream_bitrate_falls_back_to_bps_tag():
    assert m._stream_bitrate({'bit_rate': 'N/A', 'tags': {'BPS': '24577300'}}) == 24_577_300
    assert m._stream_bitrate({'tags': {'BPS-eng': '640000'}}) == 640_000


def test_bitrate_proxy_subtracts_audio():
    tracks = [track(bitrate_bps=4_000_000), track(bitrate_bps=1_000_000, idx=1)]
    s = ms(vbr=None, size=15_000_000_000, dur=6000.0, tracks=tracks)
    bps, proxy = m._video_bitrate_effective_bps(s)
    assert proxy and bps == 20_000_000 - 5_000_000


def test_dv_profile_read_from_side_data():
    v = {'side_data_list': [{'side_data_type': 'DOVI configuration record',
                             'dv_profile': 8, 'dv_bl_signal_compatibility_id': 1}]}
    assert m._dv_profile(v) == '8.1'


def test_hdr10plus_detected_from_frame_side_data():
    frames = {'frames': [{'side_data_list': [
        {'side_data_type': 'Mastering display metadata'},
        {'side_data_type': 'HDR Dynamic Metadata SMPTE2094-40 (HDR10+)'}]}]}
    assert m._frames_have_hdr10plus(frames)
    assert not m._frames_have_hdr10plus({'frames': [{'side_data_list': []}]})

