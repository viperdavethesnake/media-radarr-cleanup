# TV Show Cleanup Scripts

Two-stage pipeline for cleaning and remuxing TV shows, mirroring the movie pipeline
but adapted to season/episode structure.

Supported runtime: Python 3.13/3.14.

## Pipeline

```
tvshows/  →  tagged_tv/  →  cleaned_tv/
              ↘ failed_tv/   ↘ review_tv/
              ↘ review_tv/
```

All stage dirs and logs live under `$RC_SERVARR_DIR` (default `$RC_MEDIA_BASE/servarr` =
`/storage/media/servarr`). The scripts take no flags and can be run from any directory.

```bash
source /home/david/git/media-radarr-cleanup/.venv/bin/activate
python3 /home/david/git/media-radarr-cleanup/tv_cleanup/tv_batch_cleaner.py
python3 /home/david/git/media-radarr-cleanup/tv_cleanup/tv_mkv_remux_cleanroom.py
```

Python dependencies are the repo's shared set (`requirements.txt`).

### Stage 1 — `tv_batch_cleaner.py`

Reads `tvshows/`, writes `tagged_tv/`. 4 workers, one show per job.

- Resolves the show via the `[tvdbid-N]` in Sonarr's folder name, an IMDb id in the
  folder name, or a `uniqueid` in the source `tvshow.nfo` — in that order.
- Fetches show + per-season metadata from TMDB.
- Copies each episode (`cp --reflink=auto`), strips attachments, writes a per-episode
  `.nfo`, and injects identity tags with `mkvpropedit`.
- Downloads artwork fresh from TMDB. Sonarr's own artwork is **not** carried over —
  the source folder is deleted, and clean copies are re-fetched:
  `poster.jpg`, `fanart.jpg`, `logo.png` (English or language-neutral PNG, highest-voted;
  `.svg` logos are skipped because Jellyfin cannot read them), `seasonNN-poster.jpg`
  (`season-specials-poster.jpg` for season 0), and a per-episode `-thumb.jpg`.
  Artwork download failures are logged and non-fatal; failing to write `tvshow.nfo` or
  `metadata.json` is fatal for the show.
- Deletes the source folder only when **every** episode succeeded.

**Episode detection is `SxxExx` only.** `1x02`, `Season 1 Episode 2` and `1.02` are
*not* recognised. A file with no `SxxExx` in its name fails (it stays in the source);
the parent `Season NN` / `Specials` directory only supplies the season number. A file
whose season cannot be determined at all is moved to `review_tv/<source folder>/`.
Either way the show is marked partial.

Rejected, by design:

| Condition | Outcome |
|---|---|
| No TVDB/IMDb id, no video files, or the TMDB lookup fails | whole show → `failed_tv/` |
| TMDB `original_language != 'en'` | whole show → `failed_tv/` (foreign shows are not handled here) |
| Any other exception while processing the show | source → `failed_tv/`, partial output → `failed_tv/<source>_cleaned_partial` |
| Multi-episode file (`S01E01E02`, `S01E01-E02`) | episode fails — split it manually |
| `.mp4` source | episode fails — needs manual handling |
| Season undeterminable | file → `review_tv/`, episode counted as failed |
| Two sources resolving to the same output name | second one fails |

When some episodes fail, the partially-cleaned output is parked at
`failed_tv/<source folder>_cleaned_partial` and the source is kept for a re-run —
`tagged_tv/` must only ever hold complete shows, because stage 2 publishes whatever
it finds there.

Re-runs are resumable: an existing output is trusted only if `mkvmerge` reads it
cleanly *and* the identity tags are present (tagging is the last step, so their
presence proves the episode finished).

### Stage 2 — `tv_mkv_remux_cleanroom.py`

Reads `tagged_tv/`, writes `cleaned_tv/`. 4 workers, one episode per job, 3600 s per remux.

- Keeps one video, one audio, at most one subtitle track.
- **Audio**: English (or `und`) preferred, commentary and audio-description excluded.
  Codec priority TrueHD > DTS-HD/MA > DTS > EAC3 > AC3 > FLAC > AAC > MP3. An English
  or `und` track is relabelled `eng`; a non-English fallback keeps its real tag.
- **Subtitles**: SRT preferred, then ASS/SSA. PGS/VobSub are never kept. SDH, HI,
  forced and commentary tracks are excluded, and the kept track is flagged non-default.
- **Attachments are stripped. Chapters are KEPT** — unlike the movie remux, because on
  episodic content the markers drive intro/recap skip in Jellyfin and Infuse.
  `verify_movies.py` (which fails surviving chapters) never scans `cleaned_tv/`.
- Global tags survive the remux, so embedded identity is preserved.
- `>1 video track` or no acceptable audio → that episode (with its `.nfo` and
  `-thumb.jpg`) moves to `review_tv/<show>/<Season NN>/`. mkvmerge errors and timeouts
  leave the episode in `tagged_tv/` with the partial output removed, so a rerun retries
  it; every failed episode is listed with its reason at the end of the run. Stage 2
  never writes to `failed_tv/`.
- Per-episode `.nfo` and `-thumb.jpg` are renamed onto the new output stem so Jellyfin
  keeps matching them; show-level sidecars (`tvshow.nfo`, `metadata.json`, `poster.jpg`,
  `fanart.jpg`, `logo.png`, `seasonNN-poster.jpg`) are copied at finalize.
- The tagged source folder is deleted only when every episode succeeded.

Re-runs are resumable: an existing output is trusted only if `mkvmerge` reads it
cleanly and its duration is within 5 s of the source.

## Naming

| | |
|---|---|
| Show folder | `Ted_Lasso_(2020)/` |
| Season folder | `Season 04/` |
| Episode | `Ted_Lasso_S04E01_Home_[2160p_hevc_eac3].mkv` |

Underscores replace spaces for Jellyfin/Infuse compatibility. The
`[<res>p_<vcodec>_<acodec>]` suffix is added at the remux stage from the tracks that
were actually kept. Resolution is classified by width first, so cropped scope
releases read `2160`/`1080` rather than their raw pixel height.

## Example output

```
cleaned_tv/
└── Reacher_(2022)/
    ├── tvshow.nfo
    ├── metadata.json
    ├── poster.jpg
    ├── fanart.jpg
    ├── logo.png
    ├── season04-poster.jpg
    └── Season 04/
        ├── Reacher_S04E01_City_of_Brotherly_Love_[2160p_hevc_eac3].mkv
        ├── Reacher_S04E01_City_of_Brotherly_Love_[2160p_hevc_eac3].nfo
        └── Reacher_S04E01_City_of_Brotherly_Love_[2160p_hevc_eac3]-thumb.jpg
```

## Configuration

`TMDB_API_KEY` comes from `.env`. `RC_SERVARR_DIR` (default `$RC_MEDIA_BASE/servarr`)
locates the stage dirs and logs. `MAX_WORKERS` (4 — the pool is storage-bound; see the
concurrency note in the root `CLAUDE.md`) and the audio/subtitle preference tables are
edited in the scripts.

Logs: `$RC_SERVARR_DIR/logs/tv_batch_cleaner_debug.log` and `tv_remux_cleanroom_debug.log`.
Both scripts handle `SIGINT`/`SIGTERM` for graceful shutdown; a partial output left by
a hard kill is detected and redone on the next run.
