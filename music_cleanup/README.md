# Music Cleanup Script

One script that takes albums Lidarr has imported, converts lossless sources to ALAC with
every tag carried across, verifies the conversion is bit-exact, publishes the album to the
music folder, and tells Lidarr it is done. Same folder conventions as the movie/TV
pipelines; one stage instead of two because Lidarr delivers files already tagged from
MusicBrainz, so there is no metadata lookup step.

Supported runtime: Python 3.13/3.14.

## Pipeline

```
music/  →  cleaned_music/  →  /storage/media/music/   →  Lidarr: unmonitor + drop track records
            ↘ failed_music/
            ↘ review_music/
```

Stage dirs and the log live under `$RC_SERVARR_DIR` (default `$RC_MEDIA_BASE/servarr` =
`/storage/media/servarr`); the script can be run from any directory. Nothing in the servarr
stack triggers it.

```bash
source /home/david/git/media-radarr-cleanup/.venv/bin/activate
python3 /home/david/git/media-radarr-cleanup/music_cleanup/music_cleaner.py                 # end to end
python3 /home/david/git/media-radarr-cleanup/music_cleanup/music_cleaner.py --no-publish    # stop at cleaned_music/
python3 /home/david/git/media-radarr-cleanup/music_cleanup/music_cleaner.py --publish-only  # publish what is in cleaned_music/
python3 /home/david/git/media-radarr-cleanup/music_cleanup/music_cleaner.py --no-lidarr     # publish, leave Lidarr alone
```

`--library DIR` overrides the destination (default `$RC_MEDIA_BASE/music`). Python
dependencies are the repo's shared set (`requirements.txt`; `mutagen` is the one this
package adds). The script runs as `david`; only the permissions fix is run through
`sudo -n` (passwordless sudo is required for the default run).

### Clean: `music/` → `cleaned_music/`

Reads Lidarr's root (`Artist_[mbid-…]/Album_(Year)_[mbid-…]/`). One album folder is one
unit of work, 4 workers.

- FLAC, APE and WavPack are converted to ALAC (`.m4a`) with ffmpeg. Bit depth and sample
  rate are never changed; 24-bit stays 24-bit.
- MP3 and AAC/M4A are copied through (`cp --reflink=auto`) and not retagged.
- Tags are written with mutagen using MusicBrainz Picard's MP4 mapping: album artist,
  artist, album, title, date (falls back to original date), genre, track/disc numbers with
  totals, label, media, release type/status/country, all six MusicBrainz ids, and the
  front cover. The result is indistinguishable from a Picard-tagged ALAC file.
- Verification before the source is touched: decoded-PCM MD5 of source and output must
  match, title/album/album artist and every MusicBrainz id are read back and compared,
  cover presence must match, output count must equal source count.
- The source album folder is deleted only when every track passed. The artist folder is
  removed when it becomes empty.
- Output names are Lidarr's names with the `_[mbid-…]` tokens stripped. Ids live in tags.
- Sidecars (`.jpg`, `.cue`, `.log`, `.nfo`, `.m3u`) are not carried over; artwork is embedded.

Rejected, by design:

| Condition | Outcome |
|---|---|
| Artist or album folder lacks `[mbid-<uuid>]` | album → `failed_music/` |
| No audio, unreadable/absent tags (lossy too), OGG/Opus/WMA/WAV/other codec | album → `failed_music/` |
| ffmpeg error, PCM hash or tag mismatch, output count ≠ source count | album → `failed_music/` (partial output removed) |
| Lossless only: any file missing `musicbrainz_releasegroupid`, or it differs from the folder mbid | album → `review_music/` |
| Lossless only: files on disk ≠ `tracktotal` (summed per disc) | album → `review_music/` |
| Mixed codecs within an album | album → `review_music/` |
| Destination album already exists in `cleaned_music/` | `[SKIP]`, source left in place |

The release-group and track-count checks run only for converted (lossless) albums;
MP3/AAC copy-through albums skip them.

Re-runs are resumable for converted albums: an existing `cleaned_music/` album is trusted
(and the source removed) only if every `.m4a` opens and its `MusicBrainz Track Id` equals
the source's. A copy-through album whose destination exists is always `[SKIP]`ped.

### Publish: `cleaned_music/` → `/storage/media/music/`

Runs per album right after a successful clean (or for everything in `cleaned_music/`
with `--publish-only`).

1. Refuses to overwrite: an existing target album is `[SKIP]`ped and stays in `cleaned_music/`.
2. `sudo -n <venv python> maintenance/fix_media_perms.py --apply <album>` (no sudo when
   already root): `$RC_MEDIA_OWNER:$RC_MEDIA_GROUP` (default `david:media`), dirs 2775,
   files 664, ACLs stripped.
3. Moves the album into the artist folder under the destination; removes the emptied
   `cleaned_music/<Artist>` folder.
4. Lidarr handoff (skipped by `--no-lidarr`; a `[WARN]` and no call if the site's
   `LIDARR_URL_<33|36>` / `LIDARR_API_KEY_<33|36>` pair is unset (site picked from the
   host's IPs, `RC_SITE` overrides), the files carry no release-group id, or the album is
   unknown to Lidarr): `GET /api/v1/album?foreignAlbumId=<rg>`, `PUT /api/v1/album/{id}`
   with `monitored=false` if it was monitored, then `DELETE /api/v1/trackfile/bulk` for
   its track-file records so Lidarr neither re-downloads it nor believes the files still
   exist. A Lidarr error is logged after the album has already moved.

Lidarr refuses that delete with HTTP 409 "root folder is empty" when its root has no
subdirectories (an unmounted-drive guard; its own delete-empty-folders setting removes
the artist folder mid-operation), so the script keeps a permanent placeholder directory
`$RC_SERVARR_DIR/music/.lidarr_keep`, created on the first handoff. Do not remove it.

If the destination library directory does not exist, a publishing run aborts before
cleaning anything.

## Naming

| | |
|---|---|
| Artist folder | `Andrea_Bocelli/` |
| Album folder | `My_Christmas_(2009)/` |
| Track | `Andrea_Bocelli_-_My_Christmas_-_01_-_White_Christmas_Bianco_Natale.m4a` |

Names come from Lidarr (CleanTitle tokens, underscore separators); this script only strips
the `_[mbid-…]` folder tokens.

## Example output

```
/storage/media/music/
└── Andrea_Bocelli/
    └── My_Christmas_(2009)/
        ├── Andrea_Bocelli_-_My_Christmas_-_01_-_White_Christmas_Bianco_Natale.m4a
        ├── Andrea_Bocelli_-_My_Christmas_-_02_-_Angels_We_Have_Heard_on_High.m4a
        └── …
```

## Configuration

`LIDARR_URL` and `LIDARR_API_KEY` come from `.env` (see `.env.example`). `RC_SERVARR_DIR`
locates the stage dirs and log, `RC_MEDIA_BASE` sets the destination root,
`RC_MEDIA_OWNER`/`RC_MEDIA_GROUP` the ownership applied at publish, `RC_FFMPEG_BIN`
overrides ffmpeg. `MAX_WORKERS` (4) is edited in the script.

Log: `$RC_SERVARR_DIR/logs/music_cleaner_debug.log`. `SIGINT`/`SIGTERM` shut down gracefully.

Tests: `python -m pytest music_cleanup/tests` (synthetic FLACs + a fake Lidarr; needs ffmpeg).
