# music_cleanup — design

Date: 2026-09-26. Companion to the servarr stack spec
`/space/docker/containers/servarr/docs/superpowers/specs/2026-09-26-lidarr-design.md`.

## Goal

Take albums Lidarr has imported into its staging root (`/storage/media/servarr/music`),
convert lossless sources to ALAC, carry every tag across, verify bit-exactness, and
publish to the final library `/storage/media/music` — following the movie/TV pipeline
conventions in this repo. Scripts are run manually from `/storage/media/servarr`; nothing
in the servarr stack triggers them.

## Out of scope

- How files get from `/storage/media/music` into any player. Not this project.
- Changing Lidarr's naming, download client or indexers (done in the servarr spec).
- Scheduling, cron, systemd, or Lidarr "custom scripts". Manual invocation only.

## Part A — Lidarr quality profile `Apple Music`

Created via Lidarr API, set as root-folder default and assigned to existing artists.

| Rank | Group / quality | Allowed |
|---|---|---|
| 1 | Lossless (FLAC, FLAC 24bit, ALAC, ALAC 24bit, APE, WavPack) | yes |
| 2 | High Quality Lossy: MP3-320, MP3-VBR-V0, AAC-320, AAC-VBR | yes |
| 2 | High Quality Lossy: OGG Vorbis Q10, OPUS 192 (or any OGG/Opus) | **no** |
| 3 | Mid Quality Lossy: MP3-256, MP3-VBR-V2, AAC-256 | yes |
| 3 | Mid Quality Lossy: OGG Vorbis Q8/Q9, OPUS 128 (any OGG/Opus) | **no** |
| – | Low / Poor / Trash Quality Lossy, Unknown | no |

Cutoff: Lossless. `upgradeAllowed: true`. OGG/Opus excluded because the target
player cannot play them and lossy→lossy conversion would degrade them.
The exact quality ids inside each group are read from `GET /api/v1/qualityprofile/schema`
at apply time — never hardcoded.

## Part B — Pipeline layout

```
music/  →  cleaned_music/  →  /storage/media/music/  →  Lidarr unmonitor + drop records
            ↘ failed_music/
            ↘ review_music/
```

All paths relative to CWD `/storage/media/servarr`, like the TV pipeline. Log
`./logs/music_cleaner_debug.log`. **One script, end to end by default** (decided
2026-09-26 after review; the original two-script split is superseded):

```bash
source /home/david/git/radarr_cleanup/.venv/bin/activate
cd /storage/media/servarr
python3 /home/david/git/radarr_cleanup/music_cleanup/music_cleaner.py                # clean + publish
python3 /home/david/git/radarr_cleanup/music_cleanup/music_cleaner.py --no-publish   # stop at cleaned_music/
python3 /home/david/git/radarr_cleanup/music_cleanup/music_cleaner.py --publish-only # publish cleaned_music/ only
python3 /home/david/git/radarr_cleanup/music_cleanup/music_cleaner.py --no-lidarr    # publish, leave Lidarr alone
python3 ... --library DIR                                                           # override $RC_MEDIA_BASE/music
```

## Part C — Clean step (`music_cleaner.py`)

Pipeline-style script (argparse for the flags above, no dry-run, same scaffolding as `tv_batch_cleaner.py`:
`load_dotenv()`, `log()` to `./logs/music_cleaner_debug.log`, `timed()`, SIGINT/SIGTERM
handler with 3 s force-exit, `ThreadPoolExecutor(MAX_WORKERS=4)`, per-item try/except,
`📊 Progress: n/N albums completed`, exit code 0).

**Unit of work:** one album folder `music/<Artist_[mbid-…]>/<Album_[mbid-…]>/`.
The script walks artists, then albums; each album is one task.

**Input classification** (per file extension, case-insensitive):

| Source | Action |
|---|---|
| `.flac`, `.ape`, `.wv` | convert → `.m4a` (ALAC) |
| `.mp3`, `.m4a`, `.aac` | copy through (`cp --reflink=auto`) |
| `.ogg`, `.opus`, `.wma`, anything else audio | album → `failed_music/` |
| non-audio sidecars (`.jpg`, `.cue`, `.log`, `.nfo`, `.txt`, `.m3u`) | not copied |

**Rejected, by design:**

| Condition | Outcome |
|---|---|
| Folder name lacks `[mbid-<uuid>]` (artist or album) | album → `failed_music/` |
| Any audio file lacks `musicbrainz_releasegroupid`, or it ≠ folder mbid | album → `review_music/` |
| Track count on disk ≠ `tracktotal` tag (summed per disc when `disctotal` > 1) | album → `review_music/` |
| Mixed codecs within one album | album → `review_music/` |
| Destination album folder already exists in `cleaned_music/` | album left in place, `[SKIP]` logged |
| Undecodable audio, ffmpeg error, verification mismatch | album → `failed_music/` |
| OGG/Opus/WMA/other | album → `failed_music/` |

Moving to review/failed uses `shutil.move` of the **album** folder to
`<stage>/<Artist_folder>/<Album_folder>` (artist folder created as needed). Nothing
partial is left in `cleaned_music/`: on any failure the partial destination is removed.

**Conversion** (per file, ffmpeg from `RC_FFMPEG_BIN` or `ffmpeg` on PATH):

```
ffmpeg -nostdin -hide_banner -loglevel error -y -i <src> -map 0:a:0 -vn \
       -c:a alac -sample_fmt <s16p|s32p matching source bits> -map_metadata -1 \
       -movflags +faststart <dst>.m4a
```

`-map_metadata -1` drops ffmpeg's own tag mapping; tags are written explicitly by
mutagen (below). Bit depth and sample rate are never changed.

**Tagging** (mutagen `MP4`), using MusicBrainz Picard's MP4 mapping so the result is
identical to a Picard-tagged ALAC file:

| Vorbis comment (source) | MP4 atom (dest) |
|---|---|
| albumartist | `aART` |
| artist | `©ART` |
| album | `©alb` |
| title | `©nam` |
| date | `©day` |
| originaldate | `----:com.apple.iTunes:RELEASEDATE` |
| genre | `©gen` |
| tracknumber / tracktotal | `trkn` = (n, total) |
| discnumber / disctotal | `disk` = (n, total) |
| label | `----:com.apple.iTunes:LABEL` |
| media | `----:com.apple.iTunes:MEDIA` |
| releasetype / musicbrainz_albumtype | `----:com.apple.iTunes:MusicBrainz Album Type` |
| releasestatus / musicbrainz_albumstatus | `----:com.apple.iTunes:MusicBrainz Album Status` |
| releasecountry | `----:com.apple.iTunes:MusicBrainz Album Release Country` |
| musicbrainz_albumid | `----:com.apple.iTunes:MusicBrainz Album Id` |
| musicbrainz_releasegroupid | `----:com.apple.iTunes:MusicBrainz Release Group Id` |
| musicbrainz_artistid | `----:com.apple.iTunes:MusicBrainz Artist Id` |
| musicbrainz_albumartistid | `----:com.apple.iTunes:MusicBrainz Album Artist Id` |
| musicbrainz_trackid (recording) | `----:com.apple.iTunes:MusicBrainz Track Id` |
| musicbrainz_releasetrackid | `----:com.apple.iTunes:MusicBrainz Release Track Id` |
| first embedded picture | `covr` (MP4Cover, JPEG or PNG by mime) |
| compilation (if albumartist == "Various Artists") | `cpil` = True |

Freeform values are UTF-8 bytes. Multi-valued source tags are joined with `; `.
Copied-through MP3/M4A files are not retagged.

**Verification, before the source is deleted:**

1. PCM hash: `ffmpeg -i <f> -map 0:a:0 -vn -af aformat=sample_fmts=s32 -f md5 -`
   for source and destination must match.
2. Read the `.m4a` back with mutagen: title, album, albumartist, `trkn`, `disk` and all
   six MusicBrainz ids must equal the source values; `covr` present iff source had a
   picture.
3. Output track count == source audio count.

Only when every file in the album passes is the source album folder removed from
`music/` (`shutil.rmtree`; artist folder removed too if now empty). Failure → partial
destination deleted, source moved to `failed_music/`.

**Output naming** — same folder/file stems as Lidarr produced, with the `_[mbid-…]`
tokens stripped (ids live in tags, like movies/TV):

```
cleaned_music/
└── Andrea_Bocelli/
    └── My_Christmas_(2009)/
        ├── Andrea_Bocelli_-_My_Christmas_-_01_-_White_Christmas_Bianco_Natale.m4a
        └── …
```

**Resume:** an existing destination *file* is trusted only if mutagen opens it and its
`MusicBrainz Track Id` equals the source's; otherwise it is redone. An existing
destination *album folder* with all files trusted is skipped.

## Part D — Publish step (same script, after each successful clean)

Runs as `david`; only `maintenance/fix_media_perms.py --apply` is run through `sudo -n`
(passwordless sudo required). `--publish-only` runs this for everything already in
`cleaned_music/`.

Per album folder `cleaned_music/<Artist>/<Album>/`:

1. Target `<library>/<Artist>/<Album>/` (`--library`, else `$RC_MEDIA_BASE/music`, default
   `/storage/media/music`). If the target exists → `[SKIP]`, left in `cleaned_music/`.
   Never overwrite.
2. `sudo -n fix_media_perms.py --apply <cleaned album folder>`.
3. `shutil.move` into the target artist folder (created with 2775 and, when root,
   `RC_MEDIA_OWNER:RC_MEDIA_GROUP`). Remove the emptied `cleaned_music/<Artist>`.
4. **Lidarr handoff** (skipped by `--no-lidarr`; `[WARN]` if `LIDARR_URL`/`LIDARR_API_KEY`
   unset): read `MusicBrainz Release Group Id` from the first `.m4a`/`.mp3`;
   `GET /api/v1/album?foreignAlbumId=<rg>`; if `monitored`, `PUT monitored=false`; then
   `GET /api/v1/trackfile?albumId=` and `DELETE /api/v1/trackfile/bulk`. Not found → `[WARN]`.
   Lidarr refuses both a disk scan and a track-file delete when its root folder contains
   no subdirectories (observed 2026-09-26: `Scan folder … is empty` / HTTP 409
   `root folder … is empty`), and its delete-empty-folders setting removes the artist
   folder mid-operation. The script therefore ensures a permanent placeholder directory
   `./music/.lidarr_keep` exists before the delete. RefreshArtist is not used: with no
   audio files in the root it logs "empty" and clears nothing.

## Part E — Configuration

`.env.example` (and live `.env`) gain:

```
# Lidarr integration (music_cleanup/music_cleaner.py)
LIDARR_URL=http://192.168.33.195:8686
LIDARR_API_KEY=your_lidarr_api_key_here
```

`requirements.txt` gains `mutagen>=1.47,<2`. `.env.example` comment references `music_cleaner.py`. External binaries: `ffmpeg` (host has
8.0.1 with `alac` encoder), `cp --reflink`, `setfacl`.

## Part F — Docs

- `music_cleanup/README.md` in the shape of `tv_cleanup/README.md`: Pipeline, Stage 1,
  Stage 2, Rejected table, Naming, Example output, Configuration.
- Top-level `README.md`: tree entry, Collection Management section, Requirements.
- Repo `CLAUDE.md`: subdirectory layout and pipeline sections.
- Servarr stack spec: append Part A (profile) as a dated addendum.

## Part G — Verification (acceptance)

1. Synthetic test: an album of 3 generated FLAC files (ffmpeg `sine`, 16/44.1 and one
   24/96 variant) tagged with mutagen including all MB ids and a PNG cover, in a scratch
   CWD with `music/Test_Artist_[mbid-…]/Test_Album_(2026)_[mbid-…]/`. Running
   `music_cleaner.py` yields 3 `.m4a` in `cleaned_music/Test_Artist/Test_Album_(2026)/`,
   PCM hashes equal, all tags equal, source removed. A 4th run with a missing
   releasegroupid tag lands in `review_music/`; an `.ogg` album lands in `failed_music/`.
2. Real run on Bocelli's `My_Christmas_(2009)` in `/storage/media/servarr/music`.
3. The same run publishes it to `/storage/media/music/Andrea_Bocelli/My_Christmas_(2009)/`,
   perms `david:media` 2775/664, and Lidarr shows the album unmonitored with 0 files.
