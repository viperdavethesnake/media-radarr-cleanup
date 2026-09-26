# Radarr Cleanup Tools

A collection of tools for managing and optimizing movie and TV collections.

## 📁 Repository Structure

```
radarr_cleanup/
├── movie_cleanup/              # MKV processing and metadata tools for Movies
│   ├── batch_cleaner.py        # Extract metadata, clean names, inject tags
│   ├── mkv_remux_cleanroom.py  # Track selection and remuxing (tagged/ -> cleaned/; foreign films are foreign_cleanup/'s job)
│   ├── helpers/                # Helper scripts and utilities
│   └── README.md               # Detailed documentation
├── maintenance/                # Operational maintenance scripts for media libraries
│   ├── fix_media_perms.py       # Strip ACLs + enforce david:media + consistent perms
│   ├── orphaned_metadata_cleanup.py # Report-only: movie/doc folder completeness + leftovers
│   ├── orphaned_tv_metadata_report.py # Report-only: TV show folder completeness
│   ├── codec_bitrate_report.py  # Read-only: codec/resolution/bitrate report (ffprobe/mediainfo)
│   ├── audit_existing_libraries.py  # Audit Radarr vs filesystem library state
│   ├── compare_movie_copies.py  # Compare movie copies across libraries
│   └── regenerate_nfos.py       # Regenerate NFOs + artwork from TMDB via MKV tags
├── tv_cleanup/                 # Tools for TV Show management
│   ├── tv_batch_cleaner.py     # TV show metadata and cleaning
│   ├── tv_mkv_remux_cleanroom.py # TV show remuxing
│   └── README.md               # TV cleanup documentation
├── music_cleanup/              # Music pipeline (Lidarr → ALAC → /storage/media/music)
│   ├── music_cleaner.py        # FLAC/APE/WV → ALAC, tag carry-over, verify, publish, Lidarr handoff
│   └── README.md               # Music cleanup documentation
└── README.md                   # This file
```

## 🎬 Collection Management

### Movie Processing (`movie_cleanup/`)
Tools for processing existing Movie MKV files:
- **Metadata Extraction**: From NFO files and TMDB API
- **Name Cleaning**: Standardize folder and file names
- **Tag Injection**: Add collection and metadata tags
- **Track Selection**: Choose best audio/subtitle tracks
- **Remuxing**: Create clean, optimized MKV files

### TV Processing (`tv_cleanup/`)
Tools for processing TV Show files:
- **Episode Cleaning**: Standardize episode naming
- **Batch Processing**: Handle entire seasons or series
- **Remuxing**: Optimize tracks for TV episodes

### Music Processing (`music_cleanup/`)
Tools for processing albums imported by Lidarr:
- **ALAC Conversion**: FLAC/APE/WavPack → Apple Lossless, bit depth and rate preserved
- **Tag Carry-over**: MusicBrainz Picard MP4 mapping, all MusicBrainz ids and cover art
- **Verification**: decoded-PCM hash equality and tag read-back before the source is removed
- **Publish**: permissions, move to `/storage/media/music`, unmonitor + drop records in Lidarr (`--no-publish` to stop early)

### Maintenance (`maintenance/`)
Operational scripts for keeping the Jellyfin libraries healthy.

#### `fix_media_perms.py`
- **What it does**: Strips ACLs first, then enforces ownership `david:media` and perms (dirs `2775`, files `664`) on:
  - `/storage/media/movies`
  - `/storage/media/documentaries`
  - `/storage/media/music`
  - `/storage/media/tvshows`
- **Usage**:

```bash
./maintenance/fix_media_perms.py           # dry-run
./maintenance/fix_media_perms.py --apply   # apply (auto-sudo)
```

#### `orphaned_metadata_cleanup.py` (movies + documentaries)
- **What it does (report-only)**: Reports per-folder issues matching the movie pipeline contract:
  - folders with video but missing `movie.nfo` and/or `poster.jpg`
  - folders with `movie.nfo`/`poster.jpg` but no video in the same folder
  - folders with intermediate leftovers (`metadata.json`, `tags.xml`) next to video
- **Usage**:

```bash
./maintenance/orphaned_metadata_cleanup.py
./maintenance/orphaned_metadata_cleanup.py --csv report.csv
./maintenance/orphaned_metadata_cleanup.py --json report.json
```

#### `orphaned_tv_metadata_report.py` (TV shows)
- **What it does (report-only)**: Reports per-folder issues for TV show libraries — missing NFOs, orphaned metadata, incomplete season folders.
- **Usage**:

```bash
./maintenance/orphaned_tv_metadata_report.py
./maintenance/orphaned_tv_metadata_report.py --csv report.csv
./maintenance/orphaned_tv_metadata_report.py --json report.json
```

#### `audit_existing_libraries.py`
- **What it does**: Audits Radarr vs filesystem state — finds mismatches between what Radarr thinks exists and what's actually on disk.
- **Usage**:

```bash
./maintenance/audit_existing_libraries.py --audit
./maintenance/audit_existing_libraries.py --audit --json report.json
```

#### `compare_movie_copies.py`
- **What it does**: Compares movie copies across libraries (e.g., movies vs documentaries) — finds duplicates, codec/quality differences.
- **Usage**:

```bash
./maintenance/compare_movie_copies.py --audit
./maintenance/compare_movie_copies.py --audit --csv report.csv
```

#### `regenerate_nfos.py`
- **What it does**: Regenerates `movie.nfo`, `poster.jpg`, and `fanart.jpg` from TMDB for all movies and documentaries. Uses MKV embedded tags (TMDB/IMDB ID) as the authoritative source. Produces Jellyfin-compatible NFOs with `<uniqueid>` tags and proper collection linking (`<collectionnumber>` + `<uniqueid type="tmdbcol">`).
- **Usage**:

```bash
./maintenance/regenerate_nfos.py --audit                  # Dry-run report
./maintenance/regenerate_nfos.py --run                    # Regenerate everything
./maintenance/regenerate_nfos.py --run --workers 20       # Parallel workers
./maintenance/regenerate_nfos.py --audit --limit 10       # Quick test
./maintenance/regenerate_nfos.py --audit --json out.json  # Export to JSON
```

#### `codec_bitrate_report.py`
- **What it does (read-only)**: Generates a codec/resolution/bitrate report for media files (prefers `ffprobe`, falls back to `mediainfo`).
- **Usage**:

```bash
./maintenance/codec_bitrate_report.py --limit 10
./maintenance/codec_bitrate_report.py --csv media.csv
./maintenance/codec_bitrate_report.py --json media.json
```

#### `audit_decode_integrity.py`
- **What it does (read-only)**: Full ffmpeg software-decode of every MKV in the library, capturing decoder errors that `ffprobe` cannot surface — HEVC slice corruption ("First slice in a frame missing"), H.264 mmco/reference-frame errors, missing PPS/SPS, decode_slice_header errors, concealment warnings, etc. These are the bitstream defects that cause playback hangs/pauses but pass any header-only check.
- **Why software decode**: NVDEC silently conceals slice/reference errors to keep playback smooth. CPU `libavcodec` is the sensitive path; the audit deliberately avoids hardware acceleration.
- **Output**: incremental log + append-only JSONL + markdown summary in `$RC_SERVARR_DIR/logs/movie_decode_audit_<timestamp>.{log,jsonl,md}` (default `/storage/media/servarr/logs/`).
- **Resume**: passing `--resume <prior.jsonl>` skips MKVs already recorded as OK/ERRORS/TIMEOUT (retries FAILED/INTERRUPTED). Essential for multi-day runs.
- **Usage**:

```bash
# Full library, default 3 workers × 4 ffmpeg threads (= 12 cores, leaves Jellyfin headroom)
./maintenance/audit_decode_integrity.py

# Override scan dir + concurrency
./maintenance/audit_decode_integrity.py /storage/media/documentaries --workers 2 --threads 4

# Quick test on a handful of files
./maintenance/audit_decode_integrity.py --limit 5

# Continue a previous run after SIGINT / reboot
./maintenance/audit_decode_integrity.py --resume /storage/media/servarr/logs/movie_decode_audit_20260522_161216.jsonl
```

## 🚀 Quick Start

### Movie Processing
```bash
cd movie_cleanup
./batch_cleaner.py
./mkv_remux_cleanroom.py
```

### TV Processing
```bash
cd tv_cleanup
./tv_batch_cleaner.py
```

### Music Processing
```bash
python3 /home/david/git/radarr_cleanup/music_cleanup/music_cleaner.py   # end to end; --no-publish to stop at cleaned_music/
```

## 🔧 Requirements


- Python 3.13 or 3.14
- FFmpeg (software decoders for `maintenance/audit_decode_integrity.py`)
- `ffprobe` (typically via `ffmpeg`) or `mediainfo` (for `maintenance/codec_bitrate_report.py`)
- `setfacl` (package `acl`) for `maintenance/fix_media_perms.py`
- MKVToolNix (`mkvmerge`, `mkvpropedit`)
- TMDB API key (for metadata — set `TMDB_API_KEY` in `.env`)
- `requests` and `python-dotenv` (install via `pip install -r requirements.txt`)
- `mutagen` and FFmpeg with the `alac` encoder (for `music_cleanup/`)
- Lidarr API key (set `LIDARR_URL` / `LIDARR_API_KEY` in `.env`) for the Lidarr handoff in `music_cleanup/music_cleaner.py`

## 🌐 Multi-server Configuration

For Ubuntu 25/26 hosts with different mount layouts, use environment variables instead of editing scripts:

- `RC_MEDIA_BASE`, `RC_MEDIA_OWNER`, `RC_MEDIA_GROUP` for `maintenance/fix_media_perms.py`
- `RC_SERVARR_DIR` — the working tree every pipeline/maintenance script uses for stage dirs, logs, `nfo_backups/` and compare plans (default `$RC_MEDIA_BASE/servarr`). Scripts can be run from any directory.
- `RC_VERIFY_SCAN_DIR` for the library `verify_movies.py` / `audit_decode_integrity.py` scan
- `RC_FFMPEG_BIN`, `RC_FFPROBE_BIN` for non-PATH FFmpeg builds

## 📝 Notes

- Tools are independent and can be used separately
- MKV processing preserves original files until verified
- AV1 transcoding creates new files (original preserved)
- All tools support graceful interruption and error handling



 