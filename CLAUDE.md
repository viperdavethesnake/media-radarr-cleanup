# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Does

A media library management toolkit for cleaning and enriching MKV files destined for Jellyfin. It fetches TMDB metadata, selects optimal audio/subtitle tracks, remuxes MKVs, and verifies structure. There are parallel pipelines for movies and TV shows, a one-stage music pipeline (`music_cleanup/`), and a spec-driven manual pipeline for foreign-original films (`foreign_cleanup/PIPELINE.md`).

Pipeline order:
1. `batch_cleaner.py` / `tv_batch_cleaner.py` — TMDB metadata, NFO generation, MKV tag injection. English-original films are staged into `tagged/`. Foreign-original films (TMDB `original_language != 'en'`) are **not handled by this pipeline** — they are moved to `failed/` and managed by separate scripts / manual processes.
2. `mkv_remux_cleanroom.py` / `tv_mkv_remux_cleanroom.py` — track selection, chapter/attachment removal. Scans `tagged/` only and writes to `cleaned/`. The movie pipeline touches only `movies/` → `tagged/` → `cleaned/` (plus `failed/`/`review/`); it never reads or writes `foreign/`. All of these live under `$RC_SERVARR_DIR`.
3. `verify_movies.py` — Jellyfin compatibility validation (movies only). It is an **English-pipeline gate**: it fails anything with non-English primary audio or surviving chapters. That is intentional — a foreign film that sneaks past the batch stage should flag. Side effect: legitimate foreign_cleanup outputs staged in the shared `cleaned/` dir (which keep chapters and original-language audio by design) will also show FAILED. Those flags are expected noise, not defects — do not "fix" verify to skip them, and never auto-move flagged folders (it would yank the foreign pipeline's staged outputs).

## Running Scripts

```bash
source .venv/bin/activate   # Python 3.14 venv at repo root

# All pipeline scripts resolve stage dirs and logs from RC_SERVARR_DIR
# (default $RC_MEDIA_BASE/servarr = /storage/media/servarr). CWD does not matter.

# Movie pipeline
python3 movie_cleanup/batch_cleaner.py
python3 movie_cleanup/mkv_remux_cleanroom.py
python3 movie_cleanup/verify_movies.py --scan-dir /storage/media/servarr/cleaned

# TV pipeline
python3 tv_cleanup/tv_batch_cleaner.py
python3 tv_cleanup/tv_mkv_remux_cleanroom.py

# Music pipeline
python3 music_cleanup/music_cleaner.py              # music/ -> cleaned_music/ -> /storage/media/music + Lidarr unmonitor
python3 music_cleanup/music_cleaner.py --no-publish # stop at cleaned_music/ (--publish-only, --no-lidarr also exist)

# Maintenance
./maintenance/fix_media_perms.py              # dry-run
./maintenance/fix_media_perms.py --apply      # apply with auto-sudo
./maintenance/orphaned_metadata_cleanup.py --csv report.csv
./maintenance/audit_existing_libraries.py --movies-dir /storage/media/movies --deep
./maintenance/compare_movie_copies.py --path-a /storage/media/servarr/cleaned --path-b /storage/media/movies,/storage/media/movies2 --details
./maintenance/audit_library.py /storage/media/movies --out /tmp/audit   # full 10-check audit
./maintenance/audit_decode_integrity.py --workers 3 --threads 4         # ffmpeg full-decode pass; --resume <jsonl> to continue

```

Unit tests live in `maintenance/tests/`, `movie_cleanup/tests/` and `music_cleanup/tests/`; run `python -m pytest` from the repo root (pytest is not in `requirements.txt`). For everything else, use the helper scripts in `movie_cleanup/helpers/` to spot-check and debug (e.g., `peek_mkvinfo.py`, `spot_check_movies.py`). The old `tv_cleanup/helpers/` (hardcoded single-show migration scripts) was deleted 2026-07; recover from git history if ever needed.

## Configuration

Scripts load configuration from `.env` (not committed; see `.env.example`). All path and tool settings have sensible defaults but are overrideable:

| Variable | Purpose |
|---|---|
| `TMDB_API_KEY` | Required — TMDB API key |
| `RC_MEDIA_BASE` | Root path for media storage |
| `RC_SERVARR_DIR` | Servarr working tree for stage dirs, logs, `nfo_backups/`, compare plans (default `$RC_MEDIA_BASE/servarr`) |
| `RC_MEDIA_OWNER` / `RC_MEDIA_GROUP` | Ownership for `fix_media_perms.py` (carried through its sudo re-exec) and music publish |
| `RC_VERIFY_SCAN_DIR` | Library scanned by `verify_movies.py`, `audit_decode_integrity.py`, `audit_library.py`, `audit_content_vs_metadata.py`, `audit_title_mismatches.py` |
| `RC_FFMPEG_BIN`, `RC_FFPROBE_BIN` | Custom FFmpeg locations |
| `RADARR_URL_{33,36}`, `RADARR_API_KEY_{33,36}`, `RADARR_PROFILE_ID_{33,36}`, `RADARR_ROOT_FOLDER_{33,36}`, `RC_SITE` | `maintenance/radarr_upgrade_push.py` (one block per site; see Radarr Integration) |
| `OPENSUB_API_KEY` | `foreign_cleanup/` subtitle search |
| `LIDARR_URL_{33,36}`, `LIDARR_API_KEY_{33,36}` | `music_cleanup/music_cleaner.py` Lidarr handoff; same per-site selection as Radarr, but a missing/undecidable site only logs `[WARN]` and skips the handoff |

## System Dependencies

- **MKVToolNix**: `mkvmerge`, `mkvpropedit`, `mkvinfo`
- **FFmpeg**: with the `alac` encoder (for `music_cleanup/`) and software decoders (for `audit_decode_integrity.py`)
- **setfacl** (package `acl`) — for `fix_media_perms.py`

Python packages: `requests`, `python-dotenv`, `mutagen` (see `requirements.txt`).

## Architecture

**Concurrency**: All major scripts use `ThreadPoolExecutor`, `MAX_WORKERS = 4` in `batch_cleaner.py`, `mkv_remux_cleanroom.py`, and `tv_mkv_remux_cleanroom.py`. Despite the TMDB-lookup stage being network-bound, `batch_cleaner.py` also does local file I/O per movie (`fast_copy` into `tagged/`, `mkvmerge -J` metadata reads) against the same ZFS pool used by the remux stage, Jellyfin, Frigate, and Ollama — so it's storage-bound too, not just network-bound (originally ran at 8 workers; dropped to 4 in 2026-07 after concurrent `fast_copy`s of several 40+ GB Remux files saturated the pool and stalled an unrelated `mkvmerge -J` metadata read past its timeout). The remux stage is **storage-bound, not CPU-bound** — each `mkvmerge` is a demux/remux of a 40–90 GB file, so the bottleneck is ZFS pool bandwidth, not cores. Running too many in parallel splits that one pool N ways, collapses per-job throughput, and trips the per-job timeout (this happened at 12-way: 9 of 16 jobs timed out and landed in `failed/`). Keep all three at 4 so each job gets enough bandwidth to finish well under timeout and leaves I/O headroom for co-resident Jellyfin/Frigate/Ollama. More threads do **not** make any of these faster here. Scripts register `SIGINT`/`SIGTERM` handlers for graceful shutdown.

**File routing**: Failed files go to `failed/`, `failed_tv/`, or `failed_music/`. Files needing manual review (multiple video tracks, no acceptable audio, unparseable album folders) go to `review/`, `review_tv/`, or `review_music/`. Foreign-original sources are **not processed by this pipeline** — the movie batch step routes them to `failed/` and they are handled by separate scripts / manual work.

**Music placeholder**: `music_cleaner.py` keeps a permanent empty directory `music/.lidarr_keep` inside the Lidarr root. Lidarr refuses a track-file bulk delete with HTTP 409 "root folder is empty" when its root has no subdirectories, and its own delete-empty-folders setting removes the artist folder mid-operation, so without the placeholder the last album's handoff always fails. Do not remove it.

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

**Logging**: Dual output (console + `$RC_SERVARR_DIR/logs/*.log`, default `/storage/media/servarr/logs/`), timestamped, emoji status indicators.

**Source of truth**: MKV embedded tags are the authoritative identity for each title. For movies, those are the IMDb/TMDB IDs. For TV, each episode carries the show's TVDB/TMDB/IMDb IDs *and* per-episode S/E + episode TMDB ID. NFOs and artwork are derived from those tags and can be regenerated via `maintenance/regenerate_nfos.py`.

## Maintenance Script Conventions

Target conventions for `maintenance/` (the older scripts only partly meet them; the root README's table says which flags each one actually has):
- Read-only by default. The destructive flag is `--apply` (`fix_media_perms.py`), `--run` (`regenerate_nfos.py`, `radarr_upgrade_push.py`) or `--move-bad` (`audit_existing_libraries.py`). `regenerate_nfos.py` requires one of `--audit`/`--run` explicitly.
- `--json` / `--csv` export and `--limit N` where they make sense (not all scripts have them; `audit_library.py` uses `--out PREFIX` instead).
- `ThreadPoolExecutor` for I/O-bound scans; only `audit_decode_integrity.py` and `regenerate_nfos.py` install `SIGINT`/`SIGTERM` handlers.
- Errors accumulate and report at the end. The one script that still aborts on a single failure is `radarr_upgrade_push.py` when the initial movie-list fetch fails.
- Symlinks are skipped in every directory walk.
- Every script loads `.env`; library defaults derive from `RC_MEDIA_BASE`, the working tree from `RC_SERVARR_DIR`.

`audit_decode_integrity.py` is a long-running CPU-bound audit (full ffmpeg decode of every MKV, multi-day for a large library). It departs from a few defaults by design: no dry-run flag (it's read-only), `--resume <jsonl>` to skip files already recorded as OK/ERRORS/TIMEOUT in a prior run, JSONL appended as each decode completes so partial findings survive interruption, and conservative parallelism tuned to leave Jellyfin headroom (default `--workers 3 --threads 4` = 12 of 20 logical cores on this box). Software decode is intentional — NVDEC silently conceals the HEVC slice / H.264 mmco errors this script exists to find.

## Jellyfin Setup

- **Servers**: one per site. `192.168.36.200:8096` ("jellyfin", Docker on macvlan on barley-nas `192.168.36.40`; the setup below describes this one) and `192.168.33.200:8096` ("jellyfin-pedro", lab-server side; Movies + TV Shows libraries only).
- **Libraries**: Movies (`/storage/media/movies`), Documentaries (`/storage/media/documentaries`)
- **Metadata**: NFO reader enabled, NFO saver disabled, remote image fetchers disabled
- **Collections**: `AutomaticallyAddToCollection: true`
- **Artwork**: `poster.jpg` + `fanart.jpg` stored alongside media files
- **Hardware transcoding**: NVENC via RTX 5060 Ti (16 GB), tone mapping enabled
- **Trickplay/chapters**: generated but stored in Jellyfin data dir (not alongside media)
- **Interactive API work**: much of the Jellyfin management is done hands-on through its API rather than by a repo script. Examples: cleaning metadata, refreshing items or libraries, fixing collections. The credentials are `JELLYFIN_URL_33` / `JELLYFIN_API_KEY_33` and `JELLYFIN_URL_36` / `JELLYFIN_API_KEY_36` in `.env`; a key only works on its own server. Jellyfin 12 rejects a bare `X-Emby-Token` header (401); send `Authorization: MediaBrowser Token="<key>", Client="curl", Device="cli", DeviceId="cli", Version="1"`. No script reads these keys, and that is intended. Never remove them.

## Radarr Integration

- **Two instances, one per site**: `http://192.168.33.195:7878` (lab-server side) and `http://192.168.36.195:7878` (barley-nas side). The repo is checked out on both boxes; `.env` carries a `_33` and a `_36` block (`RADARR_URL_33`, `RADARR_API_KEY_33`, ... and the `_36` twins).
- **Site selection**: `radarr_upgrade_push.py` picks the block for the subnet the host is on, from the host's own IPv4 addresses (`192.168.33.x` → `_33`, `192.168.36.x` → `_36`). It exits if neither or both match; `RC_SITE=33|36` overrides detection in that case only. Targeting the other site's Radarr from a box is intentionally unsupported.
- **API key**: `RADARR_API_KEY_<site>` in `.env` (no fallback; the script exits if it is unset). The URL, profile id and root folder default to the live values.
- **Quality profile "Mine" (id=7)**: upgrades enabled, cutoff = Remux-2160p, allows Bluray-1080p / Remux-1080p / WEBDL-2160p / WEBRip-2160p / Bluray-2160p / Remux-2160p. Custom format scores: AV1=+100, h265=+75, h264=+50, Unwanted=-100.
- **Root folder**: `/servarr/servarr/movies`
- **Indexers**: NZBgeek + Nzb.su (Usenet, both RSS-enabled)

### Workflow: Add net-new or upgrade movies to Radarr

Use `maintenance/radarr_upgrade_push.py`. It reads saved `compare_movie_copies.py --details` output, extracts IMDb IDs for net-new titles (section 1) and `KEEP NEW` recommendations with `confidence=high` (section 3), skips anything already in Radarr, and adds the rest with profile "Mine" and `addOptions.searchForMovie` so the search starts immediately. Without `--details` there is no section 3 and only net-new titles are pushed. `compare_movie_copies.py` is read-only (the old `--apply` was removed 2026-10); its ranking rules are in its module docstring, and `REVIEW` verdicts (different runtime, no English audio, failed probe) are never pushed.

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

**Trigger search for specific movies** (bulk; not used by the script, kept for manual use):
```
POST /api/v3/command
{ "name": "MoviesSearch", "movieIds": [1, 2, 3] }
```

**Auth**: all requests require header `X-Api-Key: <key>`.

## Subdirectory Layout

```
movie_cleanup/      Main movie pipeline + helpers/ + tests/
tv_cleanup/         Main TV pipeline
music_cleanup/      Music pipeline: Lidarr staging -> ALAC -> library + tests/ (see its README)
foreign_cleanup/    Spec-driven pipeline for non-English-original films (PIPELINE.md, spec_*.json, subs/)
maintenance/        Library audits and comparisons (read-only by default), permissions, NFO regeneration, Radarr push
docs/superpowers/   Design specs and implementation plans
```
