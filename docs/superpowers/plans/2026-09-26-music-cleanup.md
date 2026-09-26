# music_cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lidarr `Apple Music` quality profile, plus `music_cleanup/music_cleaner.py` (FLAC→ALAC, tag carry-over, bit-exact verification) and `music_cleanup/music_promote.py` (perms, move to library, Lidarr unmonitor), following this repo's TV pipeline conventions.

**Architecture:** Two manually-run scripts operating on relative stage dirs under CWD `/storage/media/servarr` (`music/ → cleaned_music/ → /storage/media/music/`, with `review_music/`, `failed_music/`). Stage 1 is pipeline-style (no flags), stage 2 is maintenance-style (dry-run default, `--apply`, sudo re-exec). Verification is decoded-PCM MD5 equality plus tag read-back.

**Tech Stack:** Python 3.14 (`.venv`), mutagen, python-dotenv, requests, ffmpeg 8 (`alac`), `cp --reflink`, `maintenance/fix_media_perms.py`, Lidarr v1 API.

**Spec:** `docs/superpowers/specs/2026-09-26-music-cleanup-design.md`

## Global Constraints

- Scripts are run manually. No cron/systemd/Lidarr hooks.
- Run tests from a scratch CWD, never against `/storage/media/servarr` until Task 5.
- Never write API keys into tracked files; `.env` is untracked, `.env.example` gets placeholders.
- Follow `tv_cleanup/tv_batch_cleaner.py` scaffolding verbatim (log/timed/signal/pool/progress).
- Stage dir names: `music`, `cleaned_music`, `review_music`, `failed_music`, `logs`.
- Output names = Lidarr names with `_[mbid-<uuid>]` removed.
- Repo is git; do **not** commit unless the user asks.

---

### Task 1: Lidarr `Apple Music` quality profile

**Files:** none in repo. Applied via API from `/space/docker/containers/servarr` (`.env` there has `LIDARR_API`).

- [ ] Step 1: GET `/api/v1/qualityprofile/schema`; build items list in ascending order: Unknown(no), Trash(no), Poor(no), Low(no), then Mid group split into individual qualities — OGG Q7(no), MP3-VBR-V2(yes), MP3-256(yes), OGG Q8(no), AAC-256(yes) — then High split — MP3-VBR-V0(yes), AAC-VBR(yes), MP3-320(yes), OGG Q9(no), AAC-320(yes), OGG Q10(no) — then Lossless group(yes), WAV(no). Quality ids come from the schema, not hardcoded.
- [ ] Step 2: POST `/api/v1/qualityprofile` with `name="Apple Music"`, `upgradeAllowed=true`, `cutoff=1005`. Expected 201 with an id.
- [ ] Step 3: PUT root folder `defaultQualityProfileId` = new id; PUT each artist `qualityProfileId` = new id. Verify with GET.

### Task 2: `music_cleanup/music_cleaner.py`

**Files:** Create `music_cleanup/__init__.py` (empty), `music_cleanup/music_cleaner.py`, `music_cleanup/tests/test_music_cleaner.py`.

- [ ] Step 1: Write `tests/test_music_cleaner.py`: builds a scratch CWD with `music/Test_Artist_[mbid-<uuid4>]/Test_Album_(2026)_[mbid-<uuid4>]/` containing 3 FLACs (2× 16/44.1, 1× 24/96, ffmpeg `sine`), tagged with mutagen (all fields in spec Part C table, PNG cover), `tracktotal=3`. Runs `music_cleaner.main()` with `cwd` patched via `os.chdir`. Asserts: 3 `.m4a` in `cleaned_music/Test_Artist/Test_Album_(2026)/`, PCM md5 equal per file, every tag in the mapping equals source, `covr` present, source album gone, `logs/music_cleaner_debug.log` exists. Second test: album with one file missing `musicbrainz_releasegroupid` → in `review_music/`. Third: `.ogg` album → `failed_music/`. Fourth: `tracktotal=4` with 3 files → `review_music/`. Fifth: re-run on completed album → `[SKIP]` and no duplicate.
- [ ] Step 2: Run `.venv/bin/python -m pytest music_cleanup/tests -q` → fails (module missing). (Install pytest into the venv if absent; do not add to requirements.)
- [ ] Step 3: Implement `music_cleaner.py` per spec Part C.
- [ ] Step 4: Run tests → pass.

### Task 3: `music_cleanup/music_promote.py`

**Files:** Create `music_cleanup/music_promote.py`, `music_cleanup/tests/test_music_promote.py`.

- [ ] Step 1: Test: scratch CWD with `cleaned_music/Test_Artist/Test_Album_(2026)/x.m4a` (tagged with a release-group id), `RC_MEDIA_BASE` pointed at a scratch `media/` dir, `LIDARR_URL` pointed at a local `http.server`-based fake returning one album for `?foreignAlbumId=` and recording PUT/POST bodies. Dry run: nothing moved, `DRY-RUN:` lines present. `--apply --no-sudo --skip-perms` (test-only flags): album moved to `media/music/Test_Artist/Test_Album_(2026)/`, fake Lidarr received PUT `monitored:false` and DELETE `trackfile/bulk`; `music/.lidarr_keep` created. Existing target → not overwritten, exit 1.
- [ ] Step 2: Run → fails. Step 3: implement per spec Part D. Step 4: pass.

### Task 4: Config + docs

- [ ] `requirements.txt` has `mutagen>=1.47,<2` (done during design). `.env.example` gains `LIDARR_URL`, `LIDARR_API_KEY` block; live `.env` gains real values (key copied from `/space/docker/containers/servarr/.env`).
- [ ] `music_cleanup/README.md` (TV README shape). Top-level `README.md` tree + Collection Management + Requirements. Repo `CLAUDE.md` layout/pipeline sections.
- [ ] Servarr spec addendum for Part A.

### Task 5: Real run

- [ ] `source /home/david/git/radarr_cleanup/.venv/bin/activate && cd /storage/media/servarr && python3 /home/david/git/radarr_cleanup/music_cleanup/music_cleaner.py` on `My_Christmas_(2009)`. Verify 15 `.m4a`, hashes, tags, source gone, log.
- [ ] `music_promote.py` dry-run, then `--apply`. Verify `/storage/media/music/Andrea_Bocelli/My_Christmas_(2009)/` perms 2775/664 david:media, Lidarr album 17 `monitored=false`, 0 track files for it and no queue activity.


---

### Task 6 (added 2026-09-26): merge publish into `music_cleaner.py`

Per review: one script, end to end by default. `music_promote.py` and its tests deleted;
publish code and the fake Lidarr moved into `music_cleaner.py` / `tests/conftest.py`.
Flags `--no-publish`, `--publish-only`, `--no-lidarr`, `--library`, hidden `--skip-perms`
(tests). Perms fix runs via `sudo -n` subprocess; the script itself stays `david`.

- [x] Tests: default end-to-end (publish + Lidarr PUT/DELETE + `.lidarr_keep`), each flag,
      refuse-if-exists, missing Lidarr env → `[WARN]`. 13 pass.
- [x] Docs: package README, top README, CLAUDE.md, `.env.example`, spec Parts B/D/G.
- [x] Live: zero-album run and `--publish-only` on empty `cleaned_music/` log cleanly.
