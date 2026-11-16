"""Playback compatibility for the Pedro house (site 33) remux runs.

Used only through pedro_house/movie_remux.py and pedro_house/tv_remux.py,
which run the shared remux scripts with compat=True. That house's only client is a 2018 Samsung TV (Jellyfin Tizen app) feeding a
2010 Yamaha AVR over ARC. That chain has no DTS, no TrueHD and no Dolby
Vision, and ARC tops out at Dolby Digital 5.1. So with compat on, the remux:

- keeps an AC3 track if there is one, else EAC3 (the TV hands EAC3 to the AVR
  as Dolby Digital), else converts the chosen track to AC3;
- drops the enhancement-layer track of a dual-track Dolby Vision profile 7
  file, keeping the HDR10 base layer the TV plays;
- converts Dolby Vision profile 5 (no HDR10 base layer) to HDR10 on the GPU.

The shared scripts run directly (site 36) keep their original behaviour: best
lossless audio, multi-video files to review.
"""
import json
import os
import subprocess

FFMPEG = os.getenv('RC_FFMPEG_BIN', 'ffmpeg')
FFPROBE = os.getenv('RC_FFPROBE_BIN', 'ffprobe')


# --- audio ----------------------------------------------------------------

# Lower is better. AC3 passes through ARC untouched; EAC3 is converted to AC3
# by the TV; everything after that gets converted to AC3 by us, so the order
# there only picks the best source to convert from.
_COMPAT_RANK = {'ac3': 0, 'eac3': 1, 'truehd': 2, 'dtshd': 3, 'dts': 4,
                'flac': 5, 'pcm': 6, 'aac': 7, 'mp3': 8, 'other': 9}


def audio_family(probe):
    """probe: codec_id + codec string (see codec_probe in the remux scripts)."""
    c = (probe or '').upper()
    if 'TRUEHD' in c: return 'truehd'
    if 'DTS' in c and ('HD' in c or 'MA' in c): return 'dtshd'
    if 'DTS' in c: return 'dts'
    if 'EAC3' in c or 'E-AC-3' in c or 'EC-3' in c: return 'eac3'
    if 'AC3' in c or 'AC-3' in c: return 'ac3'
    if 'FLAC' in c: return 'flac'
    if 'PCM' in c: return 'pcm'
    if 'AAC' in c: return 'aac'
    if 'MPEG/L3' in c or 'MP3' in c: return 'mp3'
    return 'other'


def channels(track):
    return int(((track or {}).get('properties') or {}).get('audio_channels') or 0)


def pick_compat_audio(audios, probe_fn):
    """Best track for the site-33 chain from already-filtered candidates.

    A track counts as full only if it has as many channels as the AVR can use
    (5.1) or as the best candidate offers, so a stereo AC3 downmix never beats
    a 5.1 TrueHD that we can convert.
    """
    if not audios:
        return None
    target = min(6, max(channels(t) for t in audios))
    return min(audios, key=lambda t: (channels(t) < target,
                                      _COMPAT_RANK[audio_family(probe_fn(t))]))


def needs_ac3(track, probe):
    fam = audio_family(probe)
    if fam in ('ac3', 'eac3'):
        return False
    # Stereo AAC/MP3/PCM play on the TV as-is; multichannel ones would reach
    # the AVR as stereo over ARC, and DTS/TrueHD/FLAC/other don't play at all.
    if fam in ('aac', 'mp3', 'pcm') and channels(track) <= 2:
        return False
    return True


# Stand-in track for naming the output ([..._ac3]) after a conversion.
AC3_TRACK = {'type': 'audio', 'codec': 'AC-3', 'properties': {'codec_id': 'A_AC3'}}


def _ffprobe_streams(src):
    out = subprocess.run([FFPROBE, '-v', 'error', '-show_streams', '-of', 'json', src],
                         capture_output=True, text=True, check=True, timeout=300).stdout
    return json.loads(out).get('streams') or []


def convert_to_ac3(src, track, out_path, timeout=3600):
    """Encode mkvmerge track `track` of `src` to AC3 in a Matroska file.

    For Matroska input, mkvmerge track ids and ffmpeg stream indexes both
    follow track order; this is checked before encoding instead of assumed.
    """
    idx = int(track['id'])
    streams = _ffprobe_streams(src)
    if idx >= len(streams) or streams[idx].get('codec_type') != 'audio':
        raise RuntimeError(f"ffmpeg stream {idx} is not the audio track mkvmerge "
                           f"reported; refusing to guess")
    ch = channels(track) or int(streams[idx].get('channels') or 0)
    cmd = [FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
           '-i', src, '-map', f'0:{idx}', '-c:a', 'ac3',
           '-b:a', '640k' if ch > 2 else '224k']
    if ch > 6:
        cmd += ['-ac', '6']
    cmd += [out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"AC3 conversion failed: {(r.stderr or '').strip()[:500]}")


# --- video ----------------------------------------------------------------

# Dolby Vision profile 5 has no HDR10 base layer: without the DV metadata the
# picture is wrong (and at this site Jellyfin tone-mapped it live, dark).
# libplacebo applies the DV reshaping on the GPU and outputs real HDR10 (PQ,
# BT.2020, limited range), which the TV plays directly. Verified 2026-11-14 on
# A Gentleman in Moscow: matches the DV render, ~2.3x realtime on the RTX.
_DV5_TO_HDR10 = ('libplacebo=apply_dolbyvision=1:colorspace=bt2020nc:'
                 'color_primaries=bt2020:color_trc=smpte2084:range=tv:format=p010le')


def convert_dv5_to_hdr10(src, track, out_path, timeout=4 * 3600):
    """Re-encode video track `track` of `src` as HDR10 HEVC (NVENC) into `out_path`."""
    idx = int(track['id'])
    streams = _ffprobe_streams(src)
    if idx >= len(streams) or streams[idx].get('codec_type') != 'video':
        raise RuntimeError(f"ffmpeg stream {idx} is not the video track mkvmerge "
                           f"reported; refusing to guess")
    cmd = [FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
           '-init_hw_device', 'vulkan', '-i', src, '-map', f'0:{idx}',
           '-map_chapters', '-1', '-map_metadata', '-1',
           '-vf', _DV5_TO_HDR10,
           '-c:v', 'hevc_nvenc', '-profile:v', 'main10', '-preset', 'p6',
           '-rc', 'vbr', '-cq', '21', '-b:v', '0', '-maxrate', '40M', '-bufsize', '80M',
           '-color_primaries', 'bt2020', '-color_trc', 'smpte2084',
           '-colorspace', 'bt2020nc', '-color_range', 'tv', out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"DV5 -> HDR10 conversion failed: {(r.stderr or '').strip()[:500]}")


def extra_inputs(video_id, audio_id, hdr10_file=None, ac3_file=None, ac3_lang='eng'):
    """mkvmerge args to append after the source file for converted tracks.

    The source is input 0 (given --no-video / --no-audio by the caller for the
    replaced tracks); converted files follow, and --track-order keeps video
    first and audio second, with the source's subtitles after them.
    """
    args, n = [], 0
    vref, aref = f'0:{video_id}', f'0:{audio_id}'
    if hdr10_file:
        n += 1
        args += [hdr10_file]
        vref = f'{n}:0'
    if ac3_file:
        n += 1
        args += ['--language', f'0:{ac3_lang}', ac3_file]
        aref = f'{n}:0'
    if n:
        args += ['--track-order', f'{vref},{aref}']
    return args

def dv_profiles(src):
    """{stream index: Dolby Vision profile} for the video streams of `src`."""
    found = {}
    for s in _ffprobe_streams(src):
        for sd in s.get('side_data_list') or []:
            if 'dovi' in str(sd.get('side_data_type') or '').lower() \
                    and sd.get('dv_profile') is not None:
                found[int(s['index'])] = int(sd['dv_profile'])
    return found


def _width(track):
    dims = ((track.get('properties') or {}).get('pixel_dimensions') or '')
    try:
        return int(dims.lower().split('x')[0])
    except ValueError:
        return 0


def split_dv7_layers(video_tracks, profiles, probe_fn):
    """(base_layer, enhancement_layer) for a dual-track DV profile 7 file, else None.

    The enhancement layer is a second, quarter-size HEVC track; the base layer
    is plain HDR10 and is what the site-33 TV shows anyway.
    """
    if len(video_tracks) != 2:
        return None
    if not all('HEVC' in probe_fn(t).upper() for t in video_tracks):
        return None
    if 7 not in {profiles.get(int(t['id'])) for t in video_tracks}:
        return None
    big, small = sorted(video_tracks, key=_width, reverse=True)
    if _width(small) == 0 or _width(small) * 2 > _width(big):
        return None
    return big, small
