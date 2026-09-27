# Foreign Film Pipeline

The standing process for non-English-original films. The English pipeline
(`movie_cleanup/`) is never modified; its library functions are imported and its
conventions copied where they fit. The difference is control: the English pipeline
decides for itself, this one puts every decision in front of David, captured in a spec
JSON that the remux tool executes verbatim.

## Rules (policy, enforced by the spec you write, not by the tools)

1. Original-language audio, highest quality in the source — **default**
2. English text subtitle — **default**
3. English dub, highest quality, secondary — if one exists
4. Everything else dropped: other audio, other-language subs, attachments
5. Subtitles: OpenSubtitles or already-present text tracks only. **No OCR, no
   Whisper — not approved.** If no clean SRT can be had, keep the source PGS and move on.
6. Forced subtitle tracks inherit the full track's measured offset — never independently
   ffsubsync'd (sparse tracks make it produce confident garbage). `sync_subs.py`
   enforces this.
7. Chapters kept unless the spec says `keep_chapters: false`. Nothing inspects chapter
   quality; look at them yourself.
8. Per-film decisions. Tools report state; David decides. `inspect_foreign.py` emits
   observation codes only — no severity labels, no recommendations.
9. Working area is `$RC_SERVARR_DIR` (default `/storage/media/servarr`): sources in
   `foreign/`, output to `cleaned/`. The Jellyfin library and Radarr are never touched
   (`scan_library_languages.py` reads the library but writes nothing there).
10. Sources are deleted only on David's explicit instruction, after outputs verify and he
    has had the chance to spot-check.

## Stage flow (per film)

```
foreign/<Film>/
   │ 1. scan       scan_library_languages.py — which library films are non-English originals
   │ 2. stage      manual: move the film into foreign/<Film>/
   │ 3. inspect    inspect_foreign.py — read-only: tracks, TMDB identity, chapters, attachments
   │ 4. decide     David writes spec_<batch>.json (schema below)
   │ 5. subtitles  search_opensubs.py → fetch_opensubs.py → sync_subs.py
   │ 6. dry-run    remux_foreign.py spec.json — validates the spec against the actual file
   │ 7. remux      remux_foreign.py spec.json --run — one mkvmerge, then NFO/art/tags from TMDB
   │ 8. verify     manual: verify_movies.py --scan-dir .../cleaned will FAIL these by design
   │               (chapters, non-English audio); check the flags are the expected ones
   │ 9. spot-check manual: play a few points in the output
   ▼
cleaned/<Film>/        source stays in foreign/ until David says delete
```

Steps 1, 3, 5–7 are tools; 2, 4, 8, 9 and the final deletion are manual. There is no
automated verify or frame-render step.

## What "full clean" means at remux

- Only spec'd tracks survive. Video language forced to `und`; audio and subtitle
  language/default/forced flags come from the spec (video default/forced flags and any
  track names not given in the spec are inherited from the source).
- `--no-attachments` always (also sidesteps the mkvpropedit `=<uid>` gotcha).
- Container title cleared (`--title ''`). Source global tags do not carry through.
- Fresh `tags.xml` from TMDB injected via `mkvpropedit --tags all:` — embedded IMDb/TMDB
  tags are the authoritative identity (repo convention).
- `movie.nfo`, `poster.jpg`, `fanart.jpg` **regenerated from TMDB**, not copied from the
  source folder. Metadata/artwork failures are warnings, not remux failures.
- Output filename computed from the selected tracks with
  `mkv_remux_cleanroom.enhanced_file_name()`: `Title_(Year)_[<res>p_<vcodec>_<acodec>].mkv`.
  `output_name` in the spec overrides it (three older specs do this).

## Tools

All take `.env` from the repo root. `inspect`, `remux` and `scan` append to fixed-name
logs in `$RC_SERVARR_DIR/logs/`; the other three print only.

| Script | Role | Flags |
|---|---|---|
| `scan_library_languages.py` | TMDB `original_language` for every `movie.nfo` in a library; lists non-`en` | `--dir` (default `/storage/media/movies`), `--cache` (default `logs/library_languages.json`), `--workers` 4, `--limit`, `--json` |
| `inspect_foreign.py` | read-only track/identity report per film in `foreign/` | `--dir`, `--film NAME` (repeatable), `--limit`, `--json`, `--md`, `--workers` 4 |
| `search_opensubs.py` | OpenSubtitles search by IMDb id, sorted by downloads | `imdb...`, `--lang` en, `--top` 10, `--json` |
| `fetch_opensubs.py` | download by file_id, prints quota | `file_id:output_path...` (downloads immediately) |
| `sync_subs.py` | ffsubsync the full SRT to the MKV; apply that offset flat to the forced SRT | `mkv --full IN --full-out OUT [--forced IN --forced-out OUT]` |
| `remux_foreign.py` | spec-driven remux + TMDB metadata | `spec.json...`, `--run`, `--metadata-only`, `--workers` 3 |

Notes:
- `sync_subs.py` aborts (exit 2) if ffsubsync's framerate scale is not 1.0 or a
  23.976↔24 ratio, or if the last cue ends after the runtime. Needs `ffsubsync` and
  `ffprobe` on PATH and the `srt` package in the venv.
- `remux_foreign.py` validates every spec entry against `mkvmerge -J` before touching
  anything (track type, audio language and codec substring, internal sub names, external
  files exist) and skips a film on any drift. Timeout 3600 s per remux; a timeout means
  pool trouble — investigate, don't retry bigger. mkvmerge exit 1 (warnings) keeps the
  output.
- `--metadata-only` regenerates NFO/artwork/tags for films already in `cleaned/` and
  reports (does not fix) a filename that differs from the computed one. It writes
  immediately, with or without `--run`.
- Subtitles land in `foreign_cleanup/subs/<Folder>.en.srt` with the synced copy as
  `<Folder>.en.synced.srt` by convention; the tools do not enforce the names.

## Spec JSON

One file per batch (or per film), a JSON list of objects:

| Key | Required | Meaning |
|---|---|---|
| `folder` | yes | output subfolder name under `cleaned/` |
| `src_folder` | yes | absolute source directory containing exactly one MKV |
| `video` | yes | mkvmerge track id |
| `audio_default` | yes | mkvmerge track id; flagged default |
| `audio_default_lang` / `_codec` / `_name` | no | validation (`lang` exact, `codec` is a `codec_id` substring like `A_TRUEHD`) and the name to set |
| `audio_secondary` (+ `_lang` / `_codec` / `_name`) | no | second audio, non-default |
| `subtitles` | no | list of `{source_track}` or `{file}` objects with `lang` (default `eng`), `name`, `default`, `forced`, and `expect_name` for internal tracks |
| `keep_chapters` | no | default `true` |
| `imdb` | no | needed for TMDB naming and metadata; required by `--metadata-only` |
| `output_name` | no | overrides the computed filename |

`spec_ip_man.json` predates the `subtitles` list (it uses `subtitle_srt` / `sub_lang` /
`sub_name`). Re-running it as-is would produce output with no subtitles; migrate it first.

## Reused from `movie_cleanup/`

From `batch_cleaner.py`: `fetch_tmdb_metadata`, `write_nfo`, `write_tags_xml`,
`set_tags_in_mkv`, `download_image`, `find_imdbid`. From `mkv_remux_cleanroom.py`:
`enhanced_file_name`.

## History

Batches 1–4 and the Ip Man films are recorded in the `spec_*.json` files. As of
2026-07-20 all cleaned outputs from batches 1–3 were in the library (batches 1/2 in
`movies2`, batch 3 + Ip_Man_4 in `movies`) and all sources deleted. Skipped on David's
call: A_Fistful_of_Dollars and The_Good,_the_Bad_and_the_Ugly, treated as
English-canonical and left untouched. A_Private_Life was discarded (bad source copy).
