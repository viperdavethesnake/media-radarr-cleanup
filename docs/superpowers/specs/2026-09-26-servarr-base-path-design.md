# Servarr base path — design

Date: 2026-09-26. Follows the `music_cleanup` commit; separate refactor commit.

## Goal

Every script that reads or writes the servarr working tree (stage folders, logs,
`nfo_backups/`, compare plans) resolves those paths from one base directory instead of
the current working directory or the repo checkout. Scripts then behave identically
from any CWD. Every server shares the layout, so the default is `/storage/media/servarr`
and nothing needs to be set on a standard box; `.env` can override it.

## Out of scope

- Library destinations (`/storage/media/{movies,tv,music,documentaries}`). They stay on
  `RC_MEDIA_BASE` as today.
- `movie_analyzer/` (already on its own `RC_MOVIES_DIR` / `RC_WORK_DIR` / `RC_OUTPUT_DIR`).
- `maintenance/fix_media_perms.py` (operates on `RC_MEDIA_BASE`, never touches servarr).
- `maintenance/radarr_upgrade_push.py` `RADARR_ROOT_FOLDER` (a Radarr-side path).
- `movie_cleanup/helpers/scan_nfo_for_imdb.py` (`os.getcwd()` is a deliberate scan target).
- A shared Python module. Script folders stay independent; each script carries its own
  resolution line.
- A CWD fallback. If the env var is unset the default is the absolute path, never `.`.

## Resolution rule

Each affected script, near the top, after `load_dotenv()`:

```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
```

Precedence: `RC_SERVARR_DIR` → `$RC_MEDIA_BASE/servarr` → `/storage/media/servarr`.
All stage and log dirs become `os.path.join(SERVARR_DIR, '<name>')` (or
`Path(SERVARR_DIR) / '<name>'` in scripts that already use `pathlib`). Folder names
do not change. Scripts keep whichever of `os.path` / `pathlib` they already use; the
change is confined to the constants block.

Existing per-folder overrides (`RC_CLEANED_DIR`, `RC_FOREIGN_DIR`, `RC_VERIFY_SCAN_DIR`)
remain, but their defaults derive from `SERVARR_DIR` instead of being literals.

## Files

| File | Change |
|---|---|
| `movie_cleanup/batch_cleaner.py` | `SOURCE/DEST/REVIEW/FAILED/LOG_DIR` from `SERVARR_DIR` |
| `movie_cleanup/mkv_remux_cleanroom.py` | same + add `load_dotenv()` |
| `movie_cleanup/verify_movies.py` | `LOG_DIR` + add `load_dotenv()`; `RC_VERIFY_SCAN_DIR` default unchanged (library path) |
| `movie_cleanup/helpers/peek_mkvinfo.py` | `TAGGED_DIR` |
| `tv_cleanup/tv_batch_cleaner.py` | stage dirs + `LOG_DIR` |
| `tv_cleanup/tv_mkv_remux_cleanroom.py` | stage dirs + `LOG_DIR` + add `load_dotenv()` |
| `music_cleanup/music_cleaner.py` | stage dirs + `LOG_DIR`; docstring drops "Run from /storage/media/servarr" |
| `foreign_cleanup/inspect_foreign.py` | `LOG_DIR` (was `REPO_ROOT/logs`); `RC_FOREIGN_DIR` default → `SERVARR_DIR/foreign` |
| `foreign_cleanup/remux_foreign.py` | `LOG_DIR`; `RC_CLEANED_DIR` default → `SERVARR_DIR/cleaned` |
| `foreign_cleanup/scan_library_languages.py` | `LOG_DIR` and `--cache` default |
| `maintenance/regenerate_nfos.py` | `LOG_DIR`, `BACKUP_DIR` + add `load_dotenv()` |
| `maintenance/audit_decode_integrity.py` | `LOG_DIR` + add `load_dotenv()`; docstring paths |
| `maintenance/compare_movie_copies.py` | `DEFAULT_NEW_PATH`, `compare_plan_*.json` path + add `load_dotenv()`; docstring paths |
| `.env.example` | add `RC_SERVARR_DIR` with precedence comment |
| `CLAUDE.md`, `README.md`, `music_cleanup/README.md` | document base path; remove "run from /storage/media/servarr" wording |
| `music_cleanup/tests/conftest.py` | set `RC_SERVARR_DIR=tmp_path` instead of `chdir` |

`REPO_ROOT` in the foreign scripts stays because it is also used to locate `.env`.

## dotenv

Scripts listed above that lack `load_dotenv()` gain the import and call before the
constants block. python-dotenv's `find_dotenv` walks up from the calling script's own
directory (verified in `dotenv/main.py`), so the repo-root `.env` is found regardless of
CWD.

## Tests

Only `music_cleanup/tests` exists. `conftest.py` currently redirects stage dirs with
`monkeypatch.chdir(tmp_path)`. It changes to `monkeypatch.setenv('RC_SERVARR_DIR',
str(tmp_path))` followed by `importlib.reload(music_cleaner)`. The test module imports
`music_cleaner as mc` at module level, so the constants are already frozen when the
fixture runs; `reload` re-evaluates them in place and the existing `mc` reference stays
valid. All 13 tests must pass.

For the other scripts: run each from the repo root with `--help` (or its cheapest
non-destructive invocation) and confirm any log it writes lands in
`/storage/media/servarr/logs`, not `./logs`.

## Verification

- `grep -rn "'\./" --include=*.py` over the touched folders returns no stage/log paths.
- `grep -rn "REPO_ROOT, *'logs'"` returns nothing.
- `music_cleanup` tests green.
- `logs/` no longer appears in the repo root after running any maintenance script from it.

## Commit

One commit after the music commit: `refactor: resolve stage/log dirs from RC_SERVARR_DIR`.
No push. Memory note `log-path-convention.md` is rewritten to match.
