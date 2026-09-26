# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Does

A media library management toolkit for cleaning, enriching, and transcoding MKV files destined for Jellyfin. It fetches TMDB metadata, selects optimal audio/subtitle tracks, remuxes MKVs, verifies structure, and optionally transcodes to AV1. There is a parallel pipeline for movies and TV shows.

Pipeline order:
1. `batch_cleaner.py` / `tv_batch_cleaner.py` — TMDB metadata, NFO generation, MKV tag injection. English-original films are staged into `./tagged/`. Foreign-original films (TMDB `original_language != 'en'`) are **not handled by this pipeline** — they are moved to `./failed/` and managed by separate scripts / manual processes.
2. `mkv_remux_cleanroom.py` / `tv_mkv_remux_cleanroom.py` — track selection, chapter/attachment removal. Scans `./tagged/` only and writes to `./cleaned/`. The movie pipeline touches only `movies/` → `tagged/` → `cleaned/` (plus `failed/`/`review/`); it never reads or writes `./foreign/`.
3. `verify_movies.py` — Jellyfin compatibility validation (movies only). It is an **English-pipeline gate**: it fails anything with non-English primary audio or surviving chapters. That is intentional — a foreign film that sneaks past the batch stage should flag. Side effect: legitimate foreign_cleanup outputs staged in the shared `cleaned/` dir (which keep chapters and original-language audio by design) will also show FAILED. Those flags are expected noise, not defects — do not "fix" verify to skip them, and never auto-move flagged folders (it would yank the foreign pipeline's staged outputs).

## Running Scripts

```bash
source .venv/bin/activate   # Python 3.14 venv at repo root

# Movie pipeline
python3 movie_cleanup/batch_cleaner.py
python3 movie_cleanup/mkv_remux_cleanroom.py
python3 movie_cleanup/verify_movies.py --scan-dir ./cleaned

# TV pipeline
python3 tv_cleanup/tv_batch_cleaner.py
python3 tv_cleanup/tv_mkv_remux_cleanroom.py

# Music pipeline (run from /storage/media/servarr)
python3 music_cleanup/music_cleaner.py              # music/ -> cleaned_music/ -> /storage/media/music + Lidarr unmonitor
python3 music_cleanup/music_cleaner.py --no-publish # stop at cleaned_music/ (--publish-only, --no-lidarr also exist)

# Maintenance
./maintenance/fix_media_perms.py              # dry-run
./maintenance/fix_media_perms.py --apply      # apply with auto-sudo
./maintenance/orphaned_metadata_cleanup.py --csv report.csv
./maintenance/audit_existing_libraries.py /storage/media/movies
./maintenance/compare_movie_copies.py --new /path/new --existing /path/old
./maintenance/audit_library.py /storage/media/movies --out /tmp/audit   # full 10-check audit
./maintenance/audit_decode_integrity.py --workers 3 --threads 4         # ffmpeg full-decode pass; --resume <jsonl> to continue

# AV1 benchmarking (requires NVIDIA GPU)
cd movie_analyzer && ./nvidia_av1_benchmark.py
./quality_analyzer.py -r results/*.json --plot --report
```

There are no automated tests. Use the helper scripts in `movie_cleanup/helpers/` to spot-check and debug (e.g., `peek_mkvinfo.py`, `spot_check_movies.py`). The old `tv_cleanup/helpers/` (hardcoded single-show migration scripts) was deleted 2026-07; recover from git history if ever needed.

## Configuration

Scripts load configuration from `.env` (not committed; see `.env.example`). All path and tool settings have sensible defaults but are overrideable:

| Variable | Purpose |
|---|---|
| `TMDB_API_KEY` | Required — TMDB API key |
| `RC_MEDIA_BASE` | Root path for media storage |
| `RC_MEDIA_OWNER` / `RC_MEDIA_GROUP` | Ownership for `fix_media_perms.py` |
| `RC_VERIFY_SCAN_DIR`, `RC_CLEANED_DIR` | Movie script paths |
| `RC_MOVIES_DIR`, `RC_WORK_DIR`, `RC_OUTPUT_DIR` | AV1 analyzer paths |
| `RC_FFMPEG_BIN`, `RC_FFPROBE_BIN`, `RC_FFMPEG_PREFIX` | Custom FFmpeg locations |
| `LIDARR_URL`, `LIDARR_API_KEY` | `music_cleanup/music_cleaner.py` Lidarr handoff |

## System Dependencies

- **MKVToolNix**: `mkvmerge`, `mkvpropedit`, `mkvinfo`
- **FFmpeg**: with AV1 + VMAF support (for `movie_analyzer/`)
- **setfacl** (package `acl`) — for `fix_media_perms.py`
- **nvidia-smi** + CUDA — for `nvidia_av1_benchmark.py`

Python packages: `requests`, `python-dotenv`, `numpy`, `matplotlib`, `mutagen` (see `requirements.txt`).

## Architecture

**Concurrency**: All major scripts use `ThreadPoolExecutor`, `MAX_WORKERS = 4` in `batch_cleaner.py`, `mkv_remux_cleanroom.py`, and `tv_mkv_remux_cleanroom.py`. Despite the TMDB-lookup stage being network-bound, `batch_cleaner.py` also does local file I/O per movie (`fast_copy` into `./tagged`, `mkvmerge -J` metadata reads) against the same ZFS pool used by the remux stage, Jellyfin, Frigate, and Ollama — so it's storage-bound too, not just network-bound (originally ran at 8 workers; dropped to 4 in 2026-07 after concurrent `fast_copy`s of several 40+ GB Remux files saturated the pool and stalled an unrelated `mkvmerge -J` metadata read past its timeout). The remux stage is **storage-bound, not CPU-bound** — each `mkvmerge` is a demux/remux of a 40–90 GB file, so the bottleneck is ZFS pool bandwidth, not cores. Running too many in parallel splits that one pool N ways, collapses per-job throughput, and trips the per-job timeout (this happened at 12-way: 9 of 16 jobs timed out and landed in `./failed/`). Keep all three at 4 so each job gets enough bandwidth to finish well under timeout and leaves I/O headroom for co-resident Jellyfin/Frigate/Ollama. More threads do **not** make any of these faster here. Scripts register `SIGINT`/`SIGTERM` handlers for graceful shutdown.

**File routing**: Failed files go to `./failed/` or `./failed_tv/`. Files needing manual review (multiple video tracks, no acceptable audio) go to `./review/` or `./review_tv/`. Foreign-original sources are **not processed by this pipeline** — the movie batch step routes them to `./failed/` and they are handled by separate scripts / manual work.

**Metadata formats**:
- `movie.nfo` / `tvshow.nfo` / per-episode `<episodedetails>` NFO — Jellyfin-compatible XML
- `metadata.json` — intermediate processing state
- `tags.xml` — MKV tag injection via `mkvpropedit`; for TV, built per-episode (show + episode targets) and applied during batch_cleaner

> **mkvpropedit attachment-delete gotcha**: `strip_attachments` deletes cover-art attachments with the `=<uid>` selector (`--delete-attachment ={uid}`), **not** `uid:<uid>`. `uid:` is not a valid mkvpropedit selector and fails with exit 2 ("Invalid selector"); it also can't represent UIDs above signed-int64 max. Valid forms are a bare attachment id, `=<uid>`, or `name:`/`mime-type:`. Most sources have no attachments so the delete path rarely runs — the bug only surfaces on Remux releases that embed a `cover.jpg`.
- `poster.jpg` / `fanart.jpg` — artwork downloaded from TMDB

**Output naming**:
- Movies: `Movie_Title_(Year)_[2160p_hevc_eac3].mkv` inside `Movie_Title_(Year)/` (technical suffix added at remux; IMDb ID lives in embedded MKV tags, not the filename)
- TV episodes: `Show_Name_SxxExx_Episode_Title_[2160p_hevc_eac3].mkv` inside `Show_Name_(Year)/Season NN/`
- Underscores for spaces (Jellyfin/Infuse compatibility)

**Track selection rules**:
- Audio: English preferred, commentary/audio-description excluded. Codec priority: TrueHD > DTS-HD > DTS > EAC3 > AC3 > FLAC > AAC > MP3
- Subtitles: SRT preferred; SDH/HI/forced/commentary excluded
- Video: Multiple video tracks → moved to review directory
- Chapters: stripped by the **movie** remux, **kept** by the TV remux. On episodic content the markers drive intro/recap skip in Jellyfin and Infuse, so the TV script deliberately omits `--no-chapters`. `verify_movies.py` fails surviving chapters but never scans `cleaned_tv/`, so this produces no flags — do not "restore parity" here.

**TV artwork**: Sonarr's own art (`banner.jpg`, `clearlogo.png`, `seasonNN-poster.jpg`, per-episode `-thumb.jpg`) is discarded with the source folder. `tv_batch_cleaner.py` re-fetches clean copies from TMDB instead: `poster.jpg`, `fanart.jpg`, `logo.png` (English PNG only — TMDB also serves `.svg` logos, which Jellyfin cannot read), `seasonNN-poster.jpg`, and a per-episode `-thumb.jpg`. All artwork downloads are non-fatal; a transient TMDB image error must not park an otherwise-good show in `failed_tv/`. The remux renames per-episode sidecars onto the new `[2160p_hevc_eac3]` stem so Jellyfin keeps matching them.

**Logging**: Dual output (console + `./logs/*.log`), timestamped, emoji status indicators.

**Source of truth**: MKV embedded tags are the authoritative identity for each title. For movies, those are the IMDb/TMDB IDs. For TV, each episode carries the show's TVDB/TMDB/IMDb IDs *and* per-episode S/E + episode TMDB ID. NFOs and artwork are derived from those tags and can be regenerated via `maintenance/regenerate_nfos.py`.

## Maintenance Script Conventions

All scripts in `maintenance/` follow these patterns:
- Dry-run by default (`--audit`); destructive ops require `--run` or `--apply`
- `--json` / `--csv` export, `--limit N` for quick testing
- `ThreadPoolExecutor` with graceful `SIGINT`/`SIGTERM` shutdown
- Errors accumulate and report at the end — never stop mid-run on a single failure
- Symlinks skipped in all directory walks

`audit_decode_integrity.py` is a long-running CPU-bound audit (full ffmpeg decode of every MKV, multi-day for a large library). It departs from a few defaults by design: no dry-run flag (it's read-only), `--resume <jsonl>` to skip files already recorded as OK/ERRORS/TIMEOUT in a prior run, JSONL appended as each decode completes so partial findings survive interruption, and conservative parallelism tuned to leave Jellyfin headroom (default `--workers 3 --threads 4` = 12 of 20 logical cores on this box). Software decode is intentional — NVDEC silently conceals the HEVC slice / H.264 mmco errors this script exists to find.

## Jellyfin Setup

- **Server**: Docker on macvlan at `192.168.36.200:8096` (barley-nas, `192.168.36.40`)
- **Libraries**: Movies (`/storage/media/movies`), Documentaries (`/storage/media/documentaries`)
- **Metadata**: NFO reader enabled, NFO saver disabled, remote image fetchers disabled
- **Collections**: `AutomaticallyAddToCollection: true`
- **Artwork**: `poster.jpg` + `fanart.jpg` stored alongside media files
- **Hardware transcoding**: NVENC via RTX 5060 Ti (16 GB), tone mapping enabled
- **Trickplay/chapters**: generated but stored in Jellyfin data dir (not alongside media)

## Radarr Integration

- **URL**: `http://192.168.36.195:7878` (`.env` `RADARR_URL`)
- **API key**: in `.env` as `RADARR_API_KEY`. `radarr_upgrade_push.py` reads `RADARR_URL`/`RADARR_API_KEY`/`RADARR_PROFILE_ID`/`RADARR_ROOT_FOLDER` from `.env` (live values are baked in as fallback defaults).
- **Quality profile "Mine" (id=7)**: upgrades enabled, cutoff = Remux-2160p, allows Bluray-1080p / Remux-1080p / WEBDL-2160p / WEBRip-2160p / Bluray-2160p / Remux-2160p. Custom format scores: AV1=+100, h265=+75, h264=+50, Unwanted=-100.
- **Root folder**: `/servarr/servarr/movies`
- **Indexers**: NZBgeek + Nzb.su (Usenet, both RSS-enabled)

### Workflow: Add net-new or upgrade movies to Radarr

Use `maintenance/radarr_upgrade_push.py`. It reads the output of `compare_movie_copies.py`, extracts IMDb IDs for net-new and KEEP-NEW movies, and adds them to Radarr with profile "Mine" + immediate search.

```bash
# 1. Run the library comparison (both sides are remote SSH hosts)
source .venv/bin/activate
python3 maintenance/compare_movie_copies.py \
  --path-a /storage/media/movies --ssh-a david@192.168.33.40 \
  --path-b /storage/media/movies --ssh-b david@192.168.36.40 \
  --details > /tmp/compare_output.txt

# 2. Preview what will be added to Radarr (dry-run is the default)
python3 maintenance/radarr_upgrade_push.py

# 3. Push to Radarr (adds movies + triggers search immediately)
python3 maintenance/radarr_upgrade_push.py --run
```

#### Radarr API calls used (v3)

**Lookup a movie by IMDb ID** (resolves to TMDB metadata):
```
GET /api/v3/movie/lookup?term=imdb:tt0076759
```
Returns array; take `[0]`. Key fields: `title`, `year`, `tmdbId`, `imdbId`, `titleSlug`, `images`.

**Add a movie**:
```
POST /api/v3/movie
{
  "title":               "Star Wars",
  "titleSlug":           "star-wars",
  "year":                1977,
  "tmdbId":              11,
  "imdbId":              "tt0076759",
  "qualityProfileId":    7,
  "rootFolderPath":      "/servarr/servarr/movies",
  "monitored":           true,
  "minimumAvailability": "released",
  "images":              [...],          // from lookup response
  "addOptions":          { "searchForMovie": true }
}
```
Returns the created movie object including `id`.

**List all movies** (to check what's already in Radarr):
```
GET /api/v3/movie
```
Returns array of all movies. Filter by `imdbId` field to avoid duplicates.

**Trigger search for specific movies** (bulk):
```
POST /api/v3/command
{ "name": "MoviesSearch", "movieIds": [1, 2, 3] }
```

**Auth**: all requests require header `X-Api-Key: <key>`.

## Subdirectory Layout

```
movie_cleanup/      Main movie pipeline + helpers/
tv_cleanup/         Main TV pipeline
music_cleanup/      Music pipeline: Lidarr staging -> ALAC -> library (see its README)
movie_analyzer/     AV1 GPU benchmarking and quality analysis
maintenance/        Read-only reporting and permission tools
```
