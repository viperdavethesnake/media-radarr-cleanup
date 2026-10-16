# radarr_cleanup

Scripts that turn Radarr/Sonarr/Lidarr downloads into clean, tagged, Jellyfin-ready
media, plus maintenance tools for the resulting libraries. Personal tooling for one
ZFS-backed media server; paths default to that box and are overridable via `.env`.

## Layout

```
media-radarr-cleanup/
├── movie_cleanup/      batch_cleaner.py → mkv_remux_cleanroom.py → verify_movies.py  (+ helpers/)
├── tv_cleanup/         tv_batch_cleaner.py → tv_mkv_remux_cleanroom.py
├── music_cleanup/      music_cleaner.py (Lidarr → ALAC → library → Lidarr handoff)
├── foreign_cleanup/    spec-driven pipeline for non-English-original films (PIPELINE.md)
├── maintenance/        library audits, comparisons, permissions, NFO regeneration, Radarr push
├── docs/superpowers/   design specs and implementation plans
├── .env.example        every variable the scripts read
└── requirements.txt    requests, python-dotenv, mutagen
```

Each folder has its own README (`foreign_cleanup/PIPELINE.md`) with stage-by-stage
detail. `CLAUDE.md` holds the conventions and the reasoning behind them.

## Working tree

Every pipeline script reads and writes stage folders under one base directory:

```
$RC_SERVARR_DIR  (default $RC_MEDIA_BASE/servarr = /storage/media/servarr)
├── movies/ tagged/ cleaned/ review/ failed/            movie pipeline
├── tvshows/ tagged_tv/ cleaned_tv/ review_tv/ failed_tv/   TV pipeline
├── music/ cleaned_music/ review_music/ failed_music/   music pipeline
├── foreign/                                            foreign-film sources
├── logs/                                               every script's log, compare plans, audits
└── nfo_backups/                                        regenerate_nfos.py backups
```

Scripts can be run from any directory. Final libraries live under `$RC_MEDIA_BASE`
(`/storage/media/{movies,movies2,documentaries,tvshows,music}`); moving `cleaned/` into
the library is a manual step.

## Setup

```bash
cd /home/david/git/media-radarr-cleanup
python3 -m venv .venv && source .venv/bin/activate      # Python 3.13 or 3.14
pip install -r requirements.txt
cp .env.example .env                                    # then fill in the API keys
```

System tools: MKVToolNix (`mkvmerge`, `mkvpropedit`, `mkvextract`, `mkvinfo`), FFmpeg
(`ffmpeg` with the `alac` encoder, `ffprobe`), `setfacl` (package `acl`), and for
optional tools `mediainfo`, `ffsubsync`.

## Running the pipelines

```bash
source .venv/bin/activate

# Movies: movies/ -> tagged/ -> cleaned/, then verify the library
python3 movie_cleanup/batch_cleaner.py
python3 movie_cleanup/mkv_remux_cleanroom.py
python3 movie_cleanup/verify_movies.py                      # scans $RC_VERIFY_SCAN_DIR

# TV: tvshows/ -> tagged_tv/ -> cleaned_tv/
python3 tv_cleanup/tv_batch_cleaner.py
python3 tv_cleanup/tv_mkv_remux_cleanroom.py

# Music: music/ -> cleaned_music/ -> $RC_MEDIA_BASE/music + Lidarr unmonitor
python3 music_cleanup/music_cleaner.py                      # --no-publish / --publish-only / --no-lidarr / --library DIR

# Foreign films: see foreign_cleanup/PIPELINE.md
python3 foreign_cleanup/remux_foreign.py foreign_cleanup/spec_batch4.json --run
```

The batch and remux scripts take no flags. All of them run 4 workers: the stages are
storage-bound against one ZFS pool and more workers make them slower (see `CLAUDE.md`).
Foreign-original films are rejected by the movie and TV batch stages and handled by
`foreign_cleanup/` instead.

## Maintenance scripts

Read-only unless the flag in the last column is given. None of them touch the servarr
working tree except to write logs and outputs.

| Script | What it does | Destructive flag |
|---|---|---|
| `fix_media_perms.py [paths]` | strips ACLs, sets `$RC_MEDIA_OWNER:$RC_MEDIA_GROUP`, dirs 2775, files 664 on the four libraries (or given paths under `$RC_MEDIA_BASE`); re-execs under sudo | `--apply` |
| `orphaned_metadata_cleanup.py [paths] [--json] [--csv]` | movie/doc folders missing `movie.nfo`/`poster.jpg`, metadata without video, leftover `metadata.json`/`tags.xml` | none |
| `orphaned_tv_metadata_report.py [--root] [--json] [--csv]` | same three checks at the show-root level of the TV library (no season/episode checks) | none |
| `audit_existing_libraries.py [--movies-dir] [--docs-dir] [--deep]` | filesystem + NFO checks per movie folder; `--deep` adds `mkvmerge -J` track checks. Does not talk to Radarr | `--move-bad` moves flagged folders to `--move-dest` |
| `audit_library.py [scan_dir] [--out PREFIX] [--tolerance MIN]` | 10-check audit (names vs NFO vs tags, runtime vs ffprobe, MKV count, poster) → `PREFIX.md` + `.json`; default prefix `./audit_<date>` | none |
| `audit_content_vs_metadata.py [scan_dir] [--csv] [--tolerance]` | NFO runtime vs ffprobe duration, MKV TITLE tag vs NFO title | none |
| `audit_title_mismatches.py [scan_dir] [--csv]` | NFO title words vs folder name (<60% overlap flagged) | none |
| `codec_bitrate_report.py [paths] [--prefer ffprobe\|mediainfo] [--json] [--csv] [--limit]` | one row per video file: size, duration, codecs, resolution, bitrate | none |
| `audit_decode_integrity.py [scan_dir] [--workers 3] [--threads 4] [--limit] [--resume JSONL ...]` | full software decode of every MKV; logs decoder errors (HEVC slice, H.264 mmco, missing PPS/SPS, …) → `logs/movie_decode_audit_<ts>.{log,jsonl,md}`. Multi-day; `--resume` (repeatable) skips files already OK/ERRORS/TIMEOUT | none |
| `compare_movie_copies.py [--path-a] [--path-b a,b] [--ssh-a] [--ssh-b] [--details] [--strict-imdb]` | matches NEW (`cleaned/`) against EXISTING (`movies,movies2`) by IMDb id, probes both, recommends KEEP NEW / KEEP EXISTING / TIE / REVIEW: REVIEW when runtimes differ by > 2 min or English audio is missing, then resolution tier (crop ignored), HDR (DV > HDR10+ > HDR10 > SDR), remux vs encode, codec-adjusted bitrate, with audio deciding ties and small bitrate gaps; always writes `logs/compare_plan_<ts>.json` | read-only |
| `local_vs_remote_compare.py [--local] [--remote-host] [--remote-paths ...]` | local-only / remote-only / both title lists, classified Movie vs Documentary from NFO genre; no quality comparison | none |
| `regenerate_nfos.py (--audit\|--run) [--scan-dir ...] [--workers 8] [--limit] [--json] [--csv]` | rebuilds `movie.nfo`, `poster.jpg`, `fanart.jpg` from TMDB using the MKV's embedded TMDB/IMDB tags; old NFO backed up to `nfo_backups/` | `--run` |
| `radarr_upgrade_push.py [--compare-output FILE]` | reads saved `compare_movie_copies.py --details` output, adds net-new and high-confidence KEEP NEW titles to Radarr with profile "Mine" and an immediate search | `--run` |

Notes that matter:
- `fix_media_perms.py` reads `.env` and passes `RC_MEDIA_*` through its sudo re-exec.
- Every library default derives from `RC_MEDIA_BASE`; the only literal paths left are
  the remote `/mnt/media*/movies` defaults in `local_vs_remote_compare.py`, which
  describe the other machine.
- `audit_decode_integrity.py` writes a fresh timestamped set of files on every run; after
  several interruptions, pass every prior JSONL to `--resume`.

## Configuration

All variables are documented in `.env.example`. Scripts call `load_dotenv()` and find
the repo-root `.env` from any working directory.

| Variable | Used by |
|---|---|
| `TMDB_API_KEY` | movie/TV batch cleaners, foreign scripts, `regenerate_nfos.py`, `scan_library_languages.py` |
| `RC_MEDIA_BASE` | library root (default `/storage/media`); base for `RC_SERVARR_DIR` and the music destination |
| `RC_SERVARR_DIR` | working tree (default `$RC_MEDIA_BASE/servarr`) |
| `RC_MEDIA_OWNER` / `RC_MEDIA_GROUP` | `fix_media_perms.py` (shell-exported only), music publish |
| `RC_VERIFY_SCAN_DIR` | default scan dir for `verify_movies.py`, `audit_*.py`, `audit_decode_integrity.py` (default `$RC_MEDIA_BASE/movies`) |
| `RC_FFMPEG_BIN` / `RC_FFPROBE_BIN` | non-PATH FFmpeg for music, decode audit, library audits |
| `RADARR_URL` / `RADARR_API_KEY` / `RADARR_PROFILE_ID` / `RADARR_ROOT_FOLDER` | `radarr_upgrade_push.py` (API key required) |
| `OPENSUB_API_KEY` | `foreign_cleanup/search_opensubs.py`, `fetch_opensubs.py` |
| `LIDARR_URL` / `LIDARR_API_KEY` | `music_cleanup/music_cleaner.py` |

## Tests

```bash
python -m pytest music_cleanup/tests movie_cleanup/tests -q     # needs ffmpeg on PATH
```

The music suite builds synthetic FLACs and a fake Lidarr; the movie suite covers log-dir
resolution only. The pipeline scripts are otherwise exercised by running them.
