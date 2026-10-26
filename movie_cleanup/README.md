# Movie Cleanup Pipeline

Three scripts that turn a Radarr download into a Jellyfin-ready movie folder: TMDB
metadata and embedded tags, then a single-track remux, then a read-only verification of
the library.

Supported runtime: Python 3.13/3.14.

## Pipeline

```
movies/  →  tagged/  →  cleaned/            →  (manual move into /storage/media/movies)
            ↘ failed/   ↘ failed/
            ↘ review/   ↘ review/
```

All stage dirs and logs live under `$RC_SERVARR_DIR` (default `$RC_MEDIA_BASE/servarr` =
`/storage/media/servarr`). The scripts can be run from any directory.

```bash
source /home/david/git/media-radarr-cleanup/.venv/bin/activate
python3 /home/david/git/media-radarr-cleanup/movie_cleanup/batch_cleaner.py
python3 /home/david/git/media-radarr-cleanup/movie_cleanup/mkv_remux_cleanroom.py
python3 /home/david/git/media-radarr-cleanup/movie_cleanup/verify_movies.py            # scans the library
python3 /home/david/git/media-radarr-cleanup/movie_cleanup/verify_movies.py --scan-dir /storage/media/servarr/cleaned
```

`batch_cleaner.py` and `mkv_remux_cleanroom.py` take no flags. Python dependencies are the
repo's shared set (`requirements.txt`); `TMDB_API_KEY` comes from `.env`.

Foreign-original films (TMDB `original_language != 'en'`) are **not handled here**:
stage 1 routes them to `failed/` and `foreign_cleanup/` takes over.

### Stage 1 — `batch_cleaner.py`

Reads each non-hidden subfolder of `movies/`, writes `tagged/<Title>_(Year)/`. 4 workers.

- Exactly one video file per source folder. More than one → `review/`; none, or an `.mp4`
  → `failed/` (MP4 sources are not accepted).
- IMDb id (`tt` + 6–9 digits) is looked for in the filename, then `movie.nfo`
  `<uniqueid type="imdb">`, then anywhere in the NFO text. No id → `failed/`.
- TMDB `/find` must return a movie (a TV-only match fails), then `/movie/{id}` with
  credits, collection and release dates. `original_language != 'en'` → `failed/`.
- Copies the MKV with `cp --reflink=auto`, deletes every attachment with `mkvpropedit`,
  and replaces all embedded tags (`--tags all:`) with a fresh set: TITLE, YEAR, DIRECTOR,
  GENRE, IMDB, TMDB, PLOT, and COLLECTION / COLLECTION_ID when the film is in a set.
  Chapters are left alone at this stage.
- Writes `movie.nfo` (title, originaltitle, sorttitle, year, premiered, plot, tagline,
  runtime, US mpaa, TMDB rating/votes, tmdb + imdb uniqueids, genres, studios, directors,
  top-8 cast, countries, set), `metadata.json` (raw TMDB response), `tags.xml`,
  `poster.jpg` and `fanart.jpg`. A failed artwork download fails the film.
- Two sources resolving to the same output name in one run → the second goes to `review/`.
- Any error deletes the partial `tagged/` folder and moves the source to `failed/`. On
  success the source folder is deleted.

Folder and file names are `Title_(Year)`: `\ / : * ? " < > |` and `.` are removed, spaces
become `_`. The IMDb id lives in the embedded tags, not the filename.

### Stage 2 — `mkv_remux_cleanroom.py`

Reads `tagged/`, writes `cleaned/<same folder name>/`. 4 workers, 3600 s per remux.

- Keeps one video, one audio, at most one subtitle. `--no-chapters --no-attachments`.
  Global tags survive the remux, so the embedded identity is preserved.
- **Audio**: English or `und` preferred; commentary and audio-description tracks
  (by name or `flag_commentary`) are excluded. Codec priority TrueHD > DTS-HD/MA > DTS >
  EAC3 > AC3 > FLAC > AAC > MP3. If no English/`und` track exists it falls back to the
  best non-junk track of any language and keeps that language tag; `verify_movies.py`
  will then flag it. English/`und` tracks are relabelled `eng`.
- **Subtitles**: text formats only (SRT preferred, then ASS/SSA; PGS/VobSub are never
  kept), English/`und`, not SDH/HI/forced/commentary by name or flag. The kept track is
  set `eng` and non-default.
- Output name `Title_(Year)_[<res>p_<vcodec>_<acodec>].mkv`. Resolution is classified by
  width first (so cropped scope releases still read `2160`/`1080`); unknown codecs fall
  back to `unknown`.
- Only `movie.nfo`, `poster.jpg` and `fanart.jpg` are copied alongside. `metadata.json`
  and `tags.xml` stay behind and are deleted with the tagged folder.
- Cover art embedded as a still-image video track (MJPEG, or PNG in a VFW wrapper) is
  dropped by codec and not counted as a video track.
- More than one MKV, more than one real video track (e.g. a Dolby Vision enhancement
  layer), or no acceptable audio at all → `review/`. mkvmerge error, timeout, or any other exception → `failed/` (partial output
  removed). mkvmerge warnings (exit 1) are logged and the output kept.

### Stage 3 — `verify_movies.py`

Read-only. Scans every subfolder of `--scan-dir` (default `$RC_VERIFY_SCAN_DIR` =
`/storage/media/movies`, the library) and reports PASSED / WARNING / FAILED / ERROR.
Nothing is moved.

| Flag | Default | Meaning |
|---|---|---|
| `--scan-dir` | `$RC_VERIFY_SCAN_DIR` | directory of movie folders to check |
| `--threads` | 12 | worker threads (read-only, so higher than the pipeline's 4) |
| `--output FILE` | — | write detailed JSON results |
| `--quiet` / `-q` | — | hide the progress bar |

A folder **fails** on any of: folder name not `Title_(Year)` (year 1900–2030); first MKV
has ≠1 video track, >1 audio, >1 subtitle, any attachments, any chapters, zero global tags,
or a first audio track that is not English (`und` counts as not English); missing
`movie.nfo` or `poster.jpg`; NFO root not `<movie>` or missing title/year/imdbid/tmdbid.
Leftover `metadata.json` / `tags.xml` is a warning only. Exit code is 1 when anything
FAILED or ERRORed, else 0.

It is the **English-pipeline gate**: foreign_cleanup outputs staged in `cleaned/` keep
chapters and original-language audio by design and will show FAILED. That is expected
noise, not a defect. Never auto-move flagged folders.

## Helpers (`helpers/`)

None are part of the pipeline; all are single-threaded spot-check tools.

| Script | What it does |
|---|---|
| `peek_mkvinfo.py` | first 30 lines of `mkvinfo` for the first MKV in 5 random `tagged/` folders |
| `spot_check_movies.py` | compares NFO title/year/ids/plot/genre against the MKV's tags via `mediainfo --Output=JSON` for 100 random library folders |
| `audit_nfo_titles.py` | flags `movie.nfo` titles that still contain `_` or a trailing `(YYYY)` |
| `fix_nfo_titles.py` | rewrites those titles in place; dry-run unless `--apply` |
| `scan_nfo_for_imdb.py` | walks the **current directory** and writes `imdb_scan_report.csv` of IMDb ids found in NFOs |

`spot_check_movies.py`, `audit_nfo_titles.py` and `fix_nfo_titles.py` are hardcoded to
`/storage/media/movies`.

## Configuration

| Variable | Used by | Default |
|---|---|---|
| `TMDB_API_KEY` | batch_cleaner | required |
| `RC_SERVARR_DIR` | all three + peek_mkvinfo | `$RC_MEDIA_BASE/servarr` |
| `RC_MEDIA_BASE` | fallback for the above | `/storage/media` |
| `RC_VERIFY_SCAN_DIR` | verify_movies | `/storage/media/movies` |

`MAX_WORKERS` (4 in both pipeline stages) is edited in the scripts. It is deliberately
low: both stages are storage-bound against the shared ZFS pool and more workers make them
slower, not faster (see the concurrency note in the root `CLAUDE.md`).

Logs: `$RC_SERVARR_DIR/logs/batch_cleaner_debug.log`, `remux_cleanroom_debug.log`,
`verify_movies_debug.log`. The pipeline scripts handle `SIGINT`/`SIGTERM`: the first
signal stops dispatching new work and force-exits with code 1 three seconds later.

System dependencies: MKVToolNix (`mkvmerge`, `mkvpropedit`, `mkvinfo`); `mediainfo` for
`spot_check_movies.py` only.

Tests: `python -m pytest movie_cleanup/tests` (currently covers log-dir resolution only).
