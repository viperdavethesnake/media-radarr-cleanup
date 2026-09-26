# DO_THIS — integrate the new `music_cleanup/` package

A new package, `music_cleanup/`, was added to this repo from another session and is
uncommitted. Review it against this repo's conventions, fix anything that doesn't fit,
and commit it. Do not touch the unrelated uncommitted files
(`foreign_cleanup/spec_*.json`, `maintenance/local_vs_remote_compare.py`); leave them out
of the commit. Delete this file as part of the commit.

## What was added (all uncommitted)

- `music_cleanup/music_cleaner.py` — one script, end to end:
  `./music` (Lidarr root) → `./cleaned_music` → `$RC_MEDIA_BASE/music`, then Lidarr
  unmonitor + track-record delete. FLAC/APE/WV → ALAC via ffmpeg, tags carried with
  mutagen using Picard's MP4 mapping, decoded-PCM MD5 verification.
  Flags: `--no-publish`, `--publish-only`, `--no-lidarr`, `--library`.
  Runs as `david`; only `maintenance/fix_media_perms.py --apply` goes through `sudo -n`.
- `music_cleanup/README.md`
- `music_cleanup/tests/` — 13 pytest tests (synthetic FLACs + a fake Lidarr HTTP server;
  need ffmpeg on PATH).
- `docs/superpowers/specs/2026-09-26-music-cleanup-design.md`
- `docs/superpowers/plans/2026-09-26-music-cleanup.md`
- Edits to `README.md`, `CLAUDE.md`, `.env.example` (`LIDARR_URL` / `LIDARR_API_KEY`),
  `requirements.txt` (`mutagen`).

## Do this

1. Read `CLAUDE.md`, `tv_cleanup/tv_batch_cleaner.py` and `music_cleanup/README.md`, then
   review `music_cleanup/music_cleaner.py` for convention drift: `log()`/`timed()`/signal
   scaffolding, relative stage dirs from CWD `/storage/media/servarr`, `MAX_WORKERS = 4`,
   move-to-review/failed semantics, no dry-run for pipeline scripts. Flag real problems;
   don't refactor for taste.
2. Run: `source .venv/bin/activate && python -m pytest music_cleanup/tests -q`.
   All 13 must pass.
3. Check `CLAUDE.md` and `README.md` describe the music pipeline consistently with the
   script's actual flags and folder flow (`music/`, `cleaned_music/`, `review_music/`,
   `failed_music/`, `logs/music_cleaner_debug.log`, the `./music/.lidarr_keep` placeholder
   and why it exists).
4. Make sure `.env` is untracked and no API key appears in any tracked file.
5. Commit `music_cleanup/`, `docs/`, `README.md`, `CLAUDE.md`, `.env.example`,
   `requirements.txt`, and the deletion of this file, in one commit with a conventional
   message (`feat(music): ...`). Don't push.

Report what you changed, if anything, and the commit hash.
