from tv_cleanup.tv_mkv_remux_cleanroom import is_video_cover_art


def _video(codec_id, codec):
    return {'type': 'video', 'codec': codec, 'properties': {'codec_id': codec_id}}


def test_mjpeg_poster_is_cover_art():
    assert is_video_cover_art(_video('V_MJPEG', 'V_MJPEG'))


def test_vfw_png_poster_is_cover_art():
    assert is_video_cover_art(_video('V_MS/VFW/FOURCC', '0x4d504e47 "MPNG"'))


def test_real_video_is_not_cover_art():
    assert not is_video_cover_art(_video('V_MPEGH/ISO/HEVC', 'HEVC/H.265/MPEG-H'))
    assert not is_video_cover_art(_video('V_MPEG4/ISO/AVC', 'AVC/H.264/MPEG-4p10'))
