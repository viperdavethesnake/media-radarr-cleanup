# Pedro house (site 33) playback compatibility

The Pedro house has one client: a 2018 Samsung UN65NU740D (Jellyfin Tizen app)
feeding a 2010 Yamaha RX-V867 over ARC. That chain plays 4K HEVC HDR10, but
has no Dolby Vision, no AV1, no DTS and no TrueHD, and ARC carries at most
Dolby Digital 5.1. Everything else gets transcoded by Jellyfin on every play.

Run these **instead of** the shared remux scripts at this site:

```bash
source .venv/bin/activate
python3 movie_cleanup/batch_cleaner.py      # unchanged
python3 pedro_house/movie_remux.py          # instead of movie_cleanup/mkv_remux_cleanroom.py
python3 tv_cleanup/tv_batch_cleaner.py      # unchanged
python3 pedro_house/tv_remux.py             # instead of tv_cleanup/tv_mkv_remux_cleanroom.py
```

They run the shared scripts with `main(compat=True)`, which changes only:

| | Shared scripts (site 36) | Pedro house |
|---|---|---|
| Audio pick | TrueHD > DTS-HD > DTS > EAC3 > AC3 | AC3 > EAC3 > (best source to convert); a stereo track never beats a 5.1+ one |
| Audio conversion | none | DTS/TrueHD/FLAC/other, and multichannel AAC/PCM → AC3 (5.1 at 640k, 7.1 downmixed to 5.1; stereo at 224k). Stereo AAC/MP3 kept as-is |
| DV profile 7, two video tracks | review | enhancement layer dropped, HDR10 base layer kept |
| DV profile 5 (no HDR10 base) | passes through (Jellyfin tone-maps it live, dark) | converted to HDR10 on the RTX (libplacebo applies the DV metadata, hevc_nvenc 10-bit, ~2.3x realtime); the TV then plays it directly |

## Keeping the two sites apart

- This folder is the whole site-33 difference. The shared remux scripts gained only a
  `compat` switch, off by default, so running them directly behaves as it always has.
- Nothing checks which host you are on. Run the scripts above at site 33 (lab-server,
  `192.168.33.x`) and the shared remux scripts at site 36 (barley-nas, `192.168.36.x`).
  A wrong choice does not error:
  - shared remux at site 33: DTS/TrueHD/Dolby Vision files that Jellyfin has to
    transcode for this TV on every play;
  - a `pedro_house` remux at site 36: Atmos/TrueHD replaced by AC3 and Dolby Vision
    stripped, on a system that plays both. Never do this.
- To tell what a past run was, look at its log in `$RC_SERVARR_DIR/logs/`: a compat run
  starts with `Pedro house playback compatibility ON`.
- The Radarr and Sonarr "Mine" profiles at site 33 are tuned for the same TV (no Remux,
  AV1 and DV-without-HDR-fallback blocked, audio not scored). Site 36 keeps its own
  profile; the two are not meant to match. See `CLAUDE.md`, Radarr Integration.
- `maintenance/compare_movie_copies.py` ranks Dolby Vision, remux and lossless audio
  higher, so a copy made here is expected to lose to the site-36 copy of the same film.

Logic lives in `playback_compat.py`; tests in `tests/` (one end-to-end test
builds a small MKV with ffmpeg and runs the real movie remux).
