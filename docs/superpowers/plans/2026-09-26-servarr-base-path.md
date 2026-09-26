# Servarr Base Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the `DO_THIS.md` music_cleanup integration, then make every script resolve its servarr working tree (stage dirs, logs, backups, plan output) from `RC_SERVARR_DIR` instead of the CWD or the repo checkout.

**Architecture:** Each script keeps its own one-line resolution (`RC_SERVARR_DIR` → `$RC_MEDIA_BASE/servarr` → `/storage/media/servarr`) directly above its constants block; every stage/log constant is joined onto it. No shared module, no CWD fallback. Scripts that lacked `load_dotenv()` gain it so the `.env` override is honored from any CWD.

**Tech Stack:** Python 3.14, `python-dotenv`, `pytest` (music suite only), `os.path` / `pathlib` as each script already uses.

**Spec:** `docs/superpowers/specs/2026-09-26-servarr-base-path-design.md`

## Global Constraints

- Resolution line, verbatim, in every touched script (after `load_dotenv()`):
  `SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')`
- Stage folder names do not change (`movies`, `tagged`, `cleaned`, `review`, `failed`, `tvshows`, `tagged_tv`, `cleaned_tv`, `review_tv`, `failed_tv`, `music`, `cleaned_music`, `review_music`, `failed_music`, `foreign`, `logs`, `nfo_backups`).
- No CWD fallback anywhere. No `os.chdir`. No shared Python module between script folders.
- Scripts keep whatever of `os.path` / `pathlib` they already use. Only the constants block and the `load_dotenv` import change.
- `MAX_WORKERS = 4` in batch/remux/music scripts stays untouched.
- Library destinations (`/storage/media/{movies,tv,music,documentaries}`) stay on `RC_MEDIA_BASE` / `RC_VERIFY_SCAN_DIR` exactly as today.
- Out of scope: `movie_analyzer/`, `maintenance/fix_media_perms.py`, `maintenance/radarr_upgrade_push.py`, `movie_cleanup/helpers/scan_nfo_for_imdb.py`.
- No push. Two commits: `feat(music): ...` then `refactor: resolve stage/log dirs from RC_SERVARR_DIR`.
- Working tree is already checkpointed at commit `301988d`; revert to it if this goes wrong.

## Review Focus

Inputs the spec implies that no task's tests exercise, most likely to bite first. The music suite is the only test suite, so most are pinned by manual runs in Task 6.

1. `RC_SERVARR_DIR` set in `.env` but script launched from `/` or `$HOME`: `.env` must still be found. Task 6 runs `verify_movies.py --help` from `/tmp` with a scratch `RC_SERVARR_DIR` and checks the log lands there.
2. `RC_SERVARR_DIR` set to an empty string: `os.getenv` returns `''`, which is falsy, so the fallback applies rather than joining onto `''`. Task 2 adds a music test for this.
3. `RC_MEDIA_BASE` overridden but `RC_SERVARR_DIR` unset: base must become `$RC_MEDIA_BASE/servarr`. Task 2 adds a music test for this.
4. A `Path`-based script whose parent dir does not yet exist (`regenerate_nfos.py`, `audit_decode_integrity.py` use `mkdir(exist_ok=True)` without `parents=True`): on a fresh box this raises `FileNotFoundError`. Task 5 switches those to `parents=True`.
5. `music_cleaner.py` `LIDARR_KEEP_DIR` is derived from `SOURCE_DIR` at import; after the change it must be under the new absolute `SOURCE_DIR`. The existing `.lidarr_keep` test covers this once the fixture reloads the module.

---

### Task 1: Finish the DO_THIS.md music integration

**Files:**
- Modify: `CLAUDE.md:75` (file routing line), `CLAUDE.md:28-30` (music comment)
- Delete: `DO_THIS.md`
- Test: `music_cleanup/tests/` (existing 13)

**Interfaces:**
- Consumes: nothing.
- Produces: a clean `feat(music)` commit on top of checkpoint `301988d`. Task 2 onward edits `music_cleanup/` after this commit.

- [ ] **Step 1: Baseline the test suite**

Run: `cd /home/david/git/radarr_cleanup && source .venv/bin/activate && python -m pytest music_cleanup/tests -q`
Expected: `13 passed`. If anything fails, stop and report; do not proceed on a red baseline.

- [ ] **Step 2: Convention review of `music_cleaner.py` (read-only)**

Run each check and confirm the expected result. These were pre-verified while planning; the step exists so the executor confirms nothing drifted.

```bash
cd /home/david/git/radarr_cleanup
grep -n "^MAX_WORKERS" music_cleanup/music_cleaner.py          # expect: MAX_WORKERS = 4
grep -n "def signal_handler\|def log\|def timed" music_cleanup/music_cleaner.py   # expect all three
grep -n "dry.run\|--dry" music_cleanup/music_cleaner.py         # expect: no output (pipeline scripts have no dry-run)
grep -n "REVIEW_DIR\|FAILED_DIR" music_cleanup/music_cleaner.py | head   # expect: constants + _move_album callers
diff <(sed -n 30,58p tv_cleanup/tv_batch_cleaner.py) <(sed -n 75,103p music_cleanup/music_cleaner.py)
```
Expected for the `diff`: only the log filename differs (`tv_batch_cleaner_debug.log` vs `music_cleaner_debug.log`). Any other difference is a real drift; fix it to match the TV script.

- [ ] **Step 3: Secrets and `.env` check**

```bash
git check-ignore -v .env                          # expect: .gitignore:82:.env
git grep -nE "api_key\s*[=:]\s*['\"]?[a-z0-9]{20,}" -- . ':!*.example'   # expect: no output
```

- [ ] **Step 4: Document `.lidarr_keep` and the music routing folders in CLAUDE.md**

`CLAUDE.md` currently mentions neither `review_music/`/`failed_music/` nor the placeholder. Edit line 75 (the `**File routing**` paragraph). Replace:

```
**File routing**: Failed files go to `./failed/` or `./failed_tv/`. Files needing manual review (multiple video tracks, no acceptable audio) go to `./review/` or `./review_tv/`.
```
with:
```
**File routing**: Failed files go to `./failed/`, `./failed_tv/`, or `./failed_music/`. Files needing manual review (multiple video tracks, no acceptable audio, unparseable album folders) go to `./review/`, `./review_tv/`, or `./review_music/`.
```

Then append this paragraph directly after the `**File routing**` paragraph:

```
**Music placeholder**: `music_cleaner.py` keeps a permanent empty directory `music/.lidarr_keep` inside the Lidarr root. Lidarr refuses a track-file bulk delete with HTTP 409 "root folder is empty" when its root has no subdirectories, and its own delete-empty-folders setting removes the artist folder mid-operation, so without the placeholder the last album's handoff always fails. Do not remove it.
```

- [ ] **Step 5: Delete DO_THIS.md and re-run tests**

```bash
git rm -q DO_THIS.md
python -m pytest music_cleanup/tests -q
```
Expected: `13 passed`.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "feat(music): finish music_cleanup integration; document review/failed dirs and .lidarr_keep

Convention review of music_cleaner.py found no drift from tv_batch_cleaner.py
scaffolding. Adds review_music/failed_music routing and the .lidarr_keep
placeholder rationale to CLAUDE.md. Removes the DO_THIS.md task file.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: music_cleaner.py resolves from RC_SERVARR_DIR (TDD)

**Files:**
- Modify: `music_cleanup/music_cleaner.py:2-14` (docstring), `:27-31` (constants)
- Modify: `music_cleanup/tests/conftest.py:56-62` (`stage` fixture)
- Modify: `music_cleanup/tests/test_music_cleaner.py` (add two tests)

**Interfaces:**
- Consumes: nothing.
- Produces: the canonical resolution line and fixture pattern that Tasks 3-5 copy. `music_cleaner.SERVARR_DIR: str`.

- [ ] **Step 1: Change the `stage` fixture from chdir to env + reload**

Edit `music_cleanup/tests/conftest.py`. Add `import importlib` to the first line's imports, then replace the `stage` fixture:

```python
@pytest.fixture
def stage(tmp_path, monkeypatch):
    """Scratch servarr tree; RC_SERVARR_DIR points music_cleaner at it."""
    for d in ('music', 'logs'):
        (tmp_path / d).mkdir()
    monkeypatch.setenv('RC_SERVARR_DIR', str(tmp_path))
    from music_cleanup import music_cleaner
    importlib.reload(music_cleaner)
    return tmp_path
```

Why reload: `test_music_cleaner.py` does `from music_cleanup import music_cleaner as mc` at module import, before any fixture runs, so the constants are frozen. `reload` re-executes the module body in place; the existing `mc` reference sees the new values.

- [ ] **Step 2: Add the two resolution tests**

Append to `music_cleanup/tests/test_music_cleaner.py`:

```python
def test_servarr_dir_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv('RC_SERVARR_DIR', str(tmp_path))
    importlib.reload(mc)
    assert mc.SERVARR_DIR == str(tmp_path)
    assert mc.SOURCE_DIR == os.path.join(str(tmp_path), 'music')
    assert mc.LOG_DIR == os.path.join(str(tmp_path), 'logs')
    assert mc.LIDARR_KEEP_DIR == os.path.join(str(tmp_path), 'music', '.lidarr_keep')


def test_servarr_dir_falls_back_to_media_base(tmp_path, monkeypatch):
    monkeypatch.setenv('RC_SERVARR_DIR', '')
    monkeypatch.setenv('RC_MEDIA_BASE', str(tmp_path))
    importlib.reload(mc)
    assert mc.SERVARR_DIR == os.path.join(str(tmp_path), 'servarr')
    assert mc.DEST_DIR == os.path.join(str(tmp_path), 'servarr', 'cleaned_music')
```

Add `import importlib` to the top of the test file.

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest music_cleanup/tests -q`
Expected: the two new tests FAIL with `AttributeError: module ... has no attribute 'SERVARR_DIR'`. The other 13 also fail or error at this point because the fixture no longer chdirs and the script still uses `./music`. That is expected.

- [ ] **Step 4: Implement the resolution in `music_cleaner.py`**

Replace lines 27-31:

```python
SOURCE_DIR = './music'
DEST_DIR = './cleaned_music'
REVIEW_DIR = './review_music'
FAILED_DIR = './failed_music'
LOG_DIR = './logs'
```
with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
SOURCE_DIR = os.path.join(SERVARR_DIR, 'music')
DEST_DIR = os.path.join(SERVARR_DIR, 'cleaned_music')
REVIEW_DIR = os.path.join(SERVARR_DIR, 'review_music')
FAILED_DIR = os.path.join(SERVARR_DIR, 'failed_music')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
```

`LIDARR_KEEP_DIR` on line 39 already derives from `SOURCE_DIR`; leave it.

In the docstring, replace the line `Run from /storage/media/servarr.` with:
```
Stage dirs live under $RC_SERVARR_DIR (default $RC_MEDIA_BASE/servarr = /storage/media/servarr); CWD is irrelevant.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest music_cleanup/tests -q`
Expected: `15 passed`.

- [ ] **Step 6: Commit**

```bash
git add music_cleanup/music_cleaner.py music_cleanup/tests/conftest.py music_cleanup/tests/test_music_cleaner.py
git commit -m "refactor(music): resolve stage/log dirs from RC_SERVARR_DIR

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

(Intermediate commits are fine; Task 7 squashes them into the single refactor commit.)

---

### Task 3: Movie and TV pipeline scripts

**Files:**
- Modify: `movie_cleanup/batch_cleaner.py:12-16`
- Modify: `movie_cleanup/mkv_remux_cleanroom.py:3-10`
- Modify: `movie_cleanup/verify_movies.py:27-31`
- Modify: `movie_cleanup/helpers/peek_mkvinfo.py:2-6`
- Modify: `tv_cleanup/tv_batch_cleaner.py:13-17`
- Modify: `tv_cleanup/tv_mkv_remux_cleanroom.py:3-11`

**Interfaces:**
- Consumes: the resolution line from Task 2.
- Produces: nothing other tasks depend on.

No test suite exists for these. Verification is a static grep plus an import check per file.

- [ ] **Step 1: `movie_cleanup/batch_cleaner.py`**

Already has `load_dotenv()` on line 9. Replace lines 12-16:
```python
SOURCE_DIR = './movies'
DEST_DIR = './tagged'
REVIEW_DIR = './review'
FAILED_DIR = './failed'
LOG_DIR = './logs'
```
with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
SOURCE_DIR = os.path.join(SERVARR_DIR, 'movies')
DEST_DIR = os.path.join(SERVARR_DIR, 'tagged')
REVIEW_DIR = os.path.join(SERVARR_DIR, 'review')
FAILED_DIR = os.path.join(SERVARR_DIR, 'failed')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
```

- [ ] **Step 2: `movie_cleanup/mkv_remux_cleanroom.py`**

No `load_dotenv` yet. After line 4 (`from concurrent.futures import ...`) insert:
```python
from dotenv import load_dotenv

load_dotenv()
```
Then replace lines `TAGGED_DIR = './tagged'` through `LOG_DIR = './logs'` with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
TAGGED_DIR = os.path.join(SERVARR_DIR, 'tagged')
CLEANED_DIR = os.path.join(SERVARR_DIR, 'cleaned')
REVIEW_DIR = os.path.join(SERVARR_DIR, 'review')
FAILED_DIR = os.path.join(SERVARR_DIR, 'failed')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
```

- [ ] **Step 3: `movie_cleanup/verify_movies.py`**

No `load_dotenv` yet. After line 27 (`import sys`) insert:
```python
from dotenv import load_dotenv

load_dotenv()
```
Replace `LOG_DIR = './logs'` (line 31, now shifted) with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
```
`DEFAULT_SCAN_DIR` stays as-is (library path).

- [ ] **Step 4: `movie_cleanup/helpers/peek_mkvinfo.py`**

After `import subprocess` insert:
```python
from dotenv import load_dotenv

load_dotenv()
```
Replace `TAGGED_DIR = './tagged'` with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
TAGGED_DIR = os.path.join(SERVARR_DIR, 'tagged')
```

- [ ] **Step 5: `tv_cleanup/tv_batch_cleaner.py`**

Already has `load_dotenv()`. Replace lines 13-17:
```python
SOURCE_DIR = './tvshows'
DEST_DIR = './tagged_tv'
REVIEW_DIR = './review_tv'
FAILED_DIR = './failed_tv'
LOG_DIR = './logs'
```
with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
SOURCE_DIR = os.path.join(SERVARR_DIR, 'tvshows')
DEST_DIR = os.path.join(SERVARR_DIR, 'tagged_tv')
REVIEW_DIR = os.path.join(SERVARR_DIR, 'review_tv')
FAILED_DIR = os.path.join(SERVARR_DIR, 'failed_tv')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
```

- [ ] **Step 6: `tv_cleanup/tv_mkv_remux_cleanroom.py`**

No `load_dotenv` yet. After line 5 (`from concurrent.futures import ...`) insert:
```python
from dotenv import load_dotenv

load_dotenv()
```
Replace `TAGGED_DIR = './tagged_tv'` through `LOG_DIR = './logs'` with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
TAGGED_DIR = os.path.join(SERVARR_DIR, 'tagged_tv')
CLEANED_DIR = os.path.join(SERVARR_DIR, 'cleaned_tv')
REVIEW_DIR = os.path.join(SERVARR_DIR, 'review_tv')
FAILED_DIR = os.path.join(SERVARR_DIR, 'failed_tv')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
```

- [ ] **Step 7: Verify statically and by import**

```bash
cd /home/david/git/radarr_cleanup
grep -nE "= *'\./" movie_cleanup/*.py movie_cleanup/helpers/peek_mkvinfo.py tv_cleanup/*.py   # expect: no output
for f in movie_cleanup/batch_cleaner.py movie_cleanup/mkv_remux_cleanroom.py movie_cleanup/verify_movies.py tv_cleanup/tv_batch_cleaner.py tv_cleanup/tv_mkv_remux_cleanroom.py; do
  RC_SERVARR_DIR=/tmp/x python3 -c "import runpy; m=runpy.run_path('$f', run_name='x'); print('$f', m['LOG_DIR'])"
done
RC_SERVARR_DIR=/tmp/x python3 -c "import runpy; m=runpy.run_path('movie_cleanup/helpers/peek_mkvinfo.py', run_name='x'); print(m['TAGGED_DIR'])"
```
Expected: each of the five lines ends `/tmp/x/logs`; the last prints `/tmp/x/tagged`. All six scripts have `if __name__ == "__main__":` guards (verified during planning), so `run_path` with a non-main name only evaluates the constants.

- [ ] **Step 8: Commit**

```bash
git add movie_cleanup tv_cleanup
git commit -m "refactor(movie,tv): resolve stage/log dirs from RC_SERVARR_DIR

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Foreign scripts

**Files:**
- Modify: `foreign_cleanup/inspect_foreign.py:24-25`
- Modify: `foreign_cleanup/remux_foreign.py:26-27`
- Modify: `foreign_cleanup/scan_library_languages.py:22`, `:91`

**Interfaces:**
- Consumes: the resolution line from Task 2.
- Produces: nothing other tasks depend on.

All three already call `load_dotenv(os.path.join(REPO_ROOT, '.env'))`. Keep `REPO_ROOT`; it still locates `.env` and the `movie_cleanup` import path.

- [ ] **Step 1: `foreign_cleanup/inspect_foreign.py`**

Replace lines 24-25:
```python
FOREIGN_DIR = os.getenv('RC_FOREIGN_DIR', '/storage/media/servarr/foreign')
LOG_DIR = os.path.join(REPO_ROOT, 'logs')
```
with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
FOREIGN_DIR = os.getenv('RC_FOREIGN_DIR') or os.path.join(SERVARR_DIR, 'foreign')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
```

- [ ] **Step 2: `foreign_cleanup/remux_foreign.py`**

Replace lines 26-27:
```python
LOG_DIR = os.path.join(REPO_ROOT, 'logs')
CLEANED_DIR = os.getenv('RC_CLEANED_DIR', '/storage/media/servarr/cleaned')
```
with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
CLEANED_DIR = os.getenv('RC_CLEANED_DIR') or os.path.join(SERVARR_DIR, 'cleaned')
```

- [ ] **Step 3: `foreign_cleanup/scan_library_languages.py`**

Replace line 22 `LOG_DIR = os.path.join(REPO_ROOT, 'logs')` with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
```
Replace line 91 (in `main()`):
```python
    ap.add_argument('--cache', default=os.path.join(REPO_ROOT, 'logs', 'library_languages.json'))
```
with:
```python
    ap.add_argument('--cache', default=os.path.join(LOG_DIR, 'library_languages.json'))
```

- [ ] **Step 4: Verify**

```bash
grep -n "REPO_ROOT, *'logs'" foreign_cleanup/*.py                # expect: no output
grep -n "'/storage/media/servarr" foreign_cleanup/*.py            # expect: no output
for f in inspect_foreign remux_foreign scan_library_languages; do
  RC_SERVARR_DIR=/tmp/x python3 -c "import runpy; m=runpy.run_path('foreign_cleanup/$f.py', run_name='x'); print('$f', m['LOG_DIR'])"
done
```
Expected: three lines ending `/tmp/x/logs`. (`inspect_foreign` and `remux_foreign` import from `movie_cleanup`, which now also resolves; that is fine.)

- [ ] **Step 5: Commit**

```bash
git add foreign_cleanup
git commit -m "refactor(foreign): log to RC_SERVARR_DIR/logs instead of the repo checkout

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Maintenance scripts

**Files:**
- Modify: `maintenance/regenerate_nfos.py:48-49`, `:81`, `:441-443`
- Modify: `maintenance/audit_decode_integrity.py:20`, `:42`, `:82`, `:299`
- Modify: `maintenance/compare_movie_copies.py:5`, `:18`, `:47-51`, `:1480`

**Interfaces:**
- Consumes: the resolution line from Task 2.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: `maintenance/regenerate_nfos.py`**

Already has `load_dotenv()`. Replace lines 48-49:
```python
LOG_DIR = Path("./logs")
BACKUP_DIR = Path("./nfo_backups")
```
with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
LOG_DIR = Path(SERVARR_DIR) / "logs"
BACKUP_DIR = Path(SERVARR_DIR) / "nfo_backups"
```
Change both `LOG_DIR.mkdir(exist_ok=True)` calls (lines 81 and 441) and `BACKUP_DIR.mkdir(exist_ok=True)` (line 443) to add `parents=True`:
```python
    LOG_DIR.mkdir(parents=True, exist_ok=True)
```
```python
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 2: `maintenance/audit_decode_integrity.py`**

Already has `load_dotenv()`. Replace line 82 `LOG_DIR = Path('./logs')` with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
LOG_DIR = Path(SERVARR_DIR) / 'logs'
```
Line 299: `LOG_DIR.mkdir(exist_ok=True)` → `LOG_DIR.mkdir(parents=True, exist_ok=True)`.

Docstring line 20: `Outputs (under ./logs/):` → `Outputs (under $RC_SERVARR_DIR/logs/, default /storage/media/servarr/logs/):`.
Docstring line 42: `--resume logs/movie_decode_audit_<ts>.jsonl` → `--resume /storage/media/servarr/logs/movie_decode_audit_<ts>.jsonl`.

- [ ] **Step 3: `maintenance/compare_movie_copies.py`**

No `load_dotenv` yet. After line 47 (`from xml.etree import ElementTree as ET`) insert:
```python
from dotenv import load_dotenv

load_dotenv()
```
Replace line 51 `DEFAULT_NEW_PATH = "/storage/media/servarr/cleaned"` with:
```python
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
DEFAULT_NEW_PATH = os.getenv('RC_CLEANED_DIR') or os.path.join(SERVARR_DIR, 'cleaned')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
```
Replace line 1480:
```python
    plan_path = os.path.join("./logs", f"compare_plan_{time.strftime('%Y%m%d_%H%M%S')}.json")
```
with:
```python
    os.makedirs(LOG_DIR, exist_ok=True)
    plan_path = os.path.join(LOG_DIR, f"compare_plan_{time.strftime('%Y%m%d_%H%M%S')}.json")
```
Docstring line 5: `PathA (new copies):  /storage/media/servarr/cleaned` → `PathA (new copies):  $RC_SERVARR_DIR/cleaned (default /storage/media/servarr/cleaned)`.
Docstring line 18: `plan to ./logs/compare_plan_<ts>.json.` → `plan to $RC_SERVARR_DIR/logs/compare_plan_<ts>.json.`

- [ ] **Step 4: Verify**

```bash
grep -nE "\"\./|'\./" maintenance/regenerate_nfos.py maintenance/audit_decode_integrity.py maintenance/compare_movie_copies.py   # expect: no output
for f in regenerate_nfos audit_decode_integrity compare_movie_copies; do
  RC_SERVARR_DIR=/tmp/x python3 -c "import runpy; m=runpy.run_path('maintenance/$f.py', run_name='x'); print('$f', m['LOG_DIR'])"
done
```
Expected: `/tmp/x/logs` three times.

- [ ] **Step 5: Commit**

```bash
git add maintenance
git commit -m "refactor(maintenance): logs, nfo_backups and compare plans under RC_SERVARR_DIR

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Config, docs, memory, end-to-end check

**Files:**
- Modify: `.env.example:23-31`
- Modify: `CLAUDE.md:22`, `:28`, `:98`, and the Configuration table
- Modify: `README.md:149`, `:164`, `:182-186`, `:212-213`
- Modify: `music_cleanup/README.md:19-24`, `:121`
- Modify: `/home/david/.claude/projects/-home-david-git-radarr-cleanup/memory/log-path-convention.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: `.env.example`**

Replace lines 23-31:
```
# Optional: media root + ownership for maintenance/fix_media_perms.py
RC_MEDIA_BASE=/storage/media
RC_MEDIA_OWNER=david
RC_MEDIA_GROUP=media

# Optional: movie cleanup path overrides
RC_VERIFY_SCAN_DIR=/storage/media/movies
RC_FOREIGN_DIR=/storage/media/servarr/foreign
RC_CLEANED_DIR=/storage/media/servarr/cleaned
```
with:
```
# Optional: media root + ownership for maintenance/fix_media_perms.py
RC_MEDIA_BASE=/storage/media
RC_MEDIA_OWNER=david
RC_MEDIA_GROUP=media

# Optional: servarr working tree (stage dirs, logs, nfo_backups, compare plans).
# Precedence: RC_SERVARR_DIR, else $RC_MEDIA_BASE/servarr, else /storage/media/servarr.
# Scripts never fall back to the current directory.
# RC_SERVARR_DIR=/storage/media/servarr

# Optional: movie cleanup path overrides (defaults derive from RC_SERVARR_DIR)
RC_VERIFY_SCAN_DIR=/storage/media/movies
# RC_FOREIGN_DIR=/storage/media/servarr/foreign
# RC_CLEANED_DIR=/storage/media/servarr/cleaned
```

- [ ] **Step 2: `CLAUDE.md`**

Line 22: `python3 movie_cleanup/verify_movies.py --scan-dir ./cleaned` → `python3 movie_cleanup/verify_movies.py --scan-dir /storage/media/servarr/cleaned`.

Line 28: `# Music pipeline (run from /storage/media/servarr)` → `# Music pipeline`.

Directly above the `# Movie pipeline` line (line 19) insert:
```
# All pipeline scripts resolve stage dirs and logs from RC_SERVARR_DIR
# (default $RC_MEDIA_BASE/servarr = /storage/media/servarr). CWD does not matter.
```

Line 98: `**Logging**: Dual output (console + \`./logs/*.log\`), ...` → `**Logging**: Dual output (console + \`$RC_SERVARR_DIR/logs/*.log\`, default \`/storage/media/servarr/logs/\`), ...`.

In the Configuration table, add a row after `RC_MEDIA_BASE`:
```
| `RC_SERVARR_DIR` | Servarr working tree for stage dirs, logs, `nfo_backups/`, compare plans (default `$RC_MEDIA_BASE/servarr`) |
```
and change the `RC_VERIFY_SCAN_DIR, RC_CLEANED_DIR` row purpose to `Movie script overrides; `RC_CLEANED_DIR` defaults to `$RC_SERVARR_DIR/cleaned``.

- [ ] **Step 3: `README.md`**

Line 149: `` in `./logs/movie_decode_audit_<timestamp>.{log,jsonl,md}` `` → `` in `$RC_SERVARR_DIR/logs/movie_decode_audit_<timestamp>.{log,jsonl,md}` (default `/storage/media/servarr/logs/`) ``.

Line 164: `--resume logs/movie_decode_audit_20260522_161216.jsonl` → `--resume /storage/media/servarr/logs/movie_decode_audit_20260522_161216.jsonl`.

Lines 182-186, the Music Processing block: remove the `cd /storage/media/servarr` line so it reads:
```bash
python3 /home/david/git/radarr_cleanup/music_cleanup/music_cleaner.py   # end to end; --no-publish to stop at cleaned_music/
```

Lines 212-213 in Multi-server Configuration, replace:
```
- `RC_MEDIA_BASE`, `RC_MEDIA_OWNER`, `RC_MEDIA_GROUP` for `maintenance/fix_media_perms.py`
- `RC_VERIFY_SCAN_DIR`, `RC_FOREIGN_DIR`, `RC_CLEANED_DIR` for movie cleanup tools
```
with:
```
- `RC_MEDIA_BASE`, `RC_MEDIA_OWNER`, `RC_MEDIA_GROUP` for `maintenance/fix_media_perms.py`
- `RC_SERVARR_DIR` — the working tree every pipeline/maintenance script uses for stage dirs, logs, `nfo_backups/` and compare plans (default `$RC_MEDIA_BASE/servarr`). Scripts can be run from any directory.
- `RC_VERIFY_SCAN_DIR`, `RC_FOREIGN_DIR`, `RC_CLEANED_DIR` for movie cleanup tools (the last two default under `RC_SERVARR_DIR`)
```

- [ ] **Step 4: `music_cleanup/README.md`**

Lines 19-20, replace:
```
Run from `/storage/media/servarr` — every path is relative to the CWD, including `./logs`.
Nothing in the servarr stack triggers it.
```
with:
```
Stage dirs and the log live under `$RC_SERVARR_DIR` (default `$RC_MEDIA_BASE/servarr` =
`/storage/media/servarr`); the script can be run from any directory. Nothing in the servarr
stack triggers it.
```
Line 24: delete `cd /storage/media/servarr`.
Line 121: `` Log: `./logs/music_cleaner_debug.log`. `` → `` Log: `$RC_SERVARR_DIR/logs/music_cleaner_debug.log`. ``.

- [ ] **Step 5: Memory note**

Overwrite `/home/david/.claude/projects/-home-david-git-radarr-cleanup/memory/log-path-convention.md` with:
```markdown
---
name: log-path-convention
description: "All scripts resolve stage dirs and logs from RC_SERVARR_DIR (default /storage/media/servarr), never the CWD or the repo checkout"
metadata:
  type: project
---

Every pipeline, foreign, and maintenance script resolves its servarr working tree with
`SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')`
and joins stage dirs, `logs/`, `nfo_backups/` and compare plans onto it. Scripts can be run from any CWD.
Implemented 2026-09-26 (spec: docs/superpowers/specs/2026-09-26-servarr-base-path-design.md).

**Why:** The user rejected both CWD-relative (`./logs`) and repo-relative (`REPO_ROOT/logs`) paths; every server shares the `/storage/media/servarr` layout.

**How to apply:** New scripts copy the same one-liner; never add a CWD fallback, `os.chdir`, or a shared path module (script folders stay independent).
```
`MEMORY.md`'s existing pointer line still matches; leave it.

- [ ] **Step 6: End-to-end check from a foreign CWD**

```bash
cd /tmp
export RC_SERVARR_DIR=/tmp/claude-5000/-home-david-git-radarr-cleanup/33bfb00c-e384-4aa3-8195-89c54f568b0c/scratchpad/servarr_e2e
mkdir -p "$RC_SERVARR_DIR"
/home/david/git/radarr_cleanup/.venv/bin/python /home/david/git/radarr_cleanup/movie_cleanup/verify_movies.py --scan-dir "$RC_SERVARR_DIR" --quiet || true
ls "$RC_SERVARR_DIR/logs"
ls /tmp/logs /home/david/git/radarr_cleanup/logs 2>&1
unset RC_SERVARR_DIR
```
Expected: `verify_movies_debug.log` exists under `$RC_SERVARR_DIR/logs`; neither `/tmp/logs` nor the repo `logs/` was created by this run. `verify_movies.py` accepts `--scan-dir`, `--threads`, `--output`, `--quiet` only; the scan dir is empty so the run is instant.

Then run the full grep from the spec's Verification section:
```bash
cd /home/david/git/radarr_cleanup
grep -rnE "= *['\"]\./|Path\(['\"]\./" --include=*.py movie_cleanup tv_cleanup music_cleanup foreign_cleanup maintenance | grep -v scan_nfo_for_imdb   # expect: no output
grep -rn "REPO_ROOT, *'logs'" --include=*.py .                                                                                  # expect: no output
python -m pytest music_cleanup/tests -q                                                                                          # expect: 15 passed
```

- [ ] **Step 7: Commit**

```bash
git add .env.example CLAUDE.md README.md music_cleanup/README.md
git commit -m "docs: document RC_SERVARR_DIR; drop run-from-servarr wording

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Squash the refactor into one commit

**Files:** none.

**Interfaces:** none.

- [ ] **Step 1: Confirm the commit list**

Run: `git log --oneline 301988d..HEAD`
Expected, oldest first: the `feat(music)` commit from Task 1, then five `refactor`/`docs` commits from Tasks 2-6.

- [ ] **Step 2: Squash Tasks 2-6 into one commit, keeping Task 1 separate**

```bash
FEAT=$(git log --format=%H --grep='^feat(music)' 301988d..HEAD)
git reset --soft "$FEAT"
git commit -m "refactor: resolve stage/log dirs from RC_SERVARR_DIR

Every pipeline, foreign and maintenance script now derives its servarr working
tree from RC_SERVARR_DIR (default \$RC_MEDIA_BASE/servarr = /storage/media/servarr)
instead of the CWD or the repo checkout. Scripts can be run from any directory.
Adds load_dotenv() where it was missing so the .env override is honored.
Music tests switch from chdir to RC_SERVARR_DIR + reload.

Spec: docs/superpowers/specs/2026-09-26-servarr-base-path-design.md

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git log --oneline 301988d..HEAD
```
Expected: exactly two commits above `301988d`: `feat(music): ...` then `refactor: resolve stage/log dirs from RC_SERVARR_DIR`.

- [ ] **Step 3: Final verification**

```bash
git status --short          # expect: empty
python -m pytest music_cleanup/tests -q   # expect: 15 passed
```
Do not push.
