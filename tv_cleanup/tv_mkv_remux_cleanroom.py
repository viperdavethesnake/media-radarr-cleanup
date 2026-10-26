#!/usr/bin/env python3

import os, shutil, subprocess, json, time, traceback, re, signal
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
TAGGED_DIR = os.path.join(SERVARR_DIR, 'tagged_tv')
CLEANED_DIR = os.path.join(SERVARR_DIR, 'cleaned_tv')
REVIEW_DIR = os.path.join(SERVARR_DIR, 'review_tv')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')
MAX_WORKERS = 4

shutdown_requested = False


def signal_handler(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    log("⚠️ Interrupt signal received, initiating graceful shutdown...")
    import threading
    def force_exit():
        time.sleep(3)
        log("⚠️ Force exiting due to multiple interrupts...")
        os._exit(1)
    threading.Thread(target=force_exit, daemon=True).start()


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def log(msg):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {msg}"
    print(line)
    with open(os.path.join(LOG_DIR, 'tv_remux_cleanroom_debug.log'), 'a') as f:
        f.write(line + '\n')


def mkv_identify(mkv_path):
    # 300s (not 30s): metadata reads can stall behind the four concurrent
    # remuxes saturating the same ZFS pool — same lesson as batch_cleaner's
    # strip_attachments timeout.
    result = subprocess.run(['mkvmerge', '-J', mkv_path],
                            capture_output=True, text=True, check=True, timeout=300)
    return json.loads(result.stdout)


def run_mkvmerge(cmd, base, timeout):
    # mkvmerge exit codes: 0 = success, 1 = warnings (output still written),
    # 2 = fatal error. mkvmerge writes its diagnostics to stdout, not stderr.
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode == 1:
        warn = (result.stdout or '').strip()[:500]
        log(f"⚠ mkvmerge warnings on {base} (output kept): {warn}")
    elif result.returncode != 0:
        # >=2 is a fatal mkvmerge error; negative means killed by a signal
        # (Ctrl+C, OOM) with a truncated output — both are failures.
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr)


def is_eng_lang(lang):
    if not lang or lang.lower() in ('', 'und', 'en', 'eng', 'en-us', 'en-gb'):
        return True
    return False


def is_audio_junk(track):
    props = track.get('properties') or {}
    name_low = (props.get('track_name') or '').lower()
    if 'commentary' in name_low or props.get('flag_commentary'):
        return True
    if 'description' in name_low or 'descriptive' in name_low or 'visually impaired' in name_low:
        return True
    return False


def is_sub_junk(track):
    props = track.get('properties') or {}
    name_low = (props.get('track_name') or '').lower()
    if (
        'commentary' in name_low
        or 'sdh' in name_low
        or 'hearing' in name_low
        or 'impaired' in name_low
        or re.search(r'\bhi\b', name_low)   # word-boundary: catches "English (HI)"
                                              # without excluding e.g. "Sushi Party"
        or 'forced' in name_low
        or props.get('flag_commentary')
        or props.get('flag_hearing_impaired')
        or props.get('forced_track')
    ):
        return True
    return False


def is_video_cover_art(track):
    # Some releases embed the poster as a still-image "video" track (MJPEG, or
    # PNG in a VFW wrapper) instead of an attachment. No real episode video
    # uses these codecs, so the codec alone is a safe signal.
    c = codec_probe(track).upper()
    return 'MJPEG' in c or 'MJPG' in c or 'MPNG' in c or 'PNG ' in c

def codec_probe(track):
    """Combined codec-id + human-readable codec string for rank/slug matching.

    Matroska's codec_id alone can't distinguish DTS variants — every one of
    them is A_DTS, so "DTS-HD MA" is only visible in mkvmerge's `codec` field.
    MP4 sources have no codec_id at all. Combining both covers both gaps.
    """
    p = track.get('properties') or {}
    return f"{p.get('codec_id') or ''} {track.get('codec') or ''}"

def audio_codec_rank(codec_id):
    # Lower is better. EAC3 > AC3 (modern preference).
    c = (codec_id or '').upper()
    if 'TRUEHD' in c: return 0
    if 'DTS' in c and ('HD' in c or 'MA' in c): return 1
    if 'DTS' in c: return 2
    if 'EAC3' in c or 'E-AC-3' in c or 'EC-3' in c: return 3
    if 'AC3' in c or 'AC-3' in c: return 4
    if 'FLAC' in c: return 5
    if 'AAC' in c: return 6
    if 'MPEG/L3' in c or 'MP3' in c: return 7
    return 99


def track_codec_short(track):
    # mkvmerge -J fills `properties.codec_id` for Matroska sources only; for
    # MP4 it leaves codec_id=None and exposes a human-readable `codec` field
    # at the track top level. Fall through to that so the codec slug in the
    # output filename is sensible even for non-Matroska-origin tracks.
    return codec_id_short(codec_probe(track))


def codec_id_short(codec_id):
    c = (codec_id or '').upper()
    # Video
    if 'HEVC' in c or 'H.265' in c: return 'hevc'
    if 'AVC' in c or 'H.264' in c: return 'h264'
    if 'V_AV1' in c or c == 'V_AV1': return 'av1'
    if 'VP9' in c: return 'vp9'
    if 'MPEG2' in c: return 'mpeg2'
    # Audio (EAC3 check must precede AC3)
    if 'TRUEHD' in c: return 'truehd'
    if 'DTS' in c and ('HD' in c or 'MA' in c): return 'dtshd'
    if 'DTS' in c: return 'dts'
    if 'EAC3' in c or 'E-AC-3' in c or 'EC-3' in c: return 'eac3'
    if 'AC3' in c or 'AC-3' in c: return 'ac3'
    if 'FLAC' in c: return 'flac'
    if 'AAC' in c: return 'aac'
    if 'MPEG/L3' in c or 'MP3' in c: return 'mp3'
    return 'unknown'


def pick_best_audio(tracks):
    audios = [t for t in tracks
              if t['type'] == 'audio'
              and is_eng_lang((t.get('properties') or {}).get('language'))
              and not is_audio_junk(t)]
    if not audios:
        audios = [t for t in tracks if t['type'] == 'audio' and not is_audio_junk(t)]
    if not audios:
        return None
    audios.sort(key=lambda t: audio_codec_rank(codec_probe(t)))
    return audios[0]


def pick_best_subtitle(tracks):
    allowed = ['S_TEXT/UTF8', 'S_TEXT/ASS', 'S_TEXT/SSA']
    valid = [t for t in tracks
             if t['type'] == 'subtitles'
             and is_eng_lang((t.get('properties') or {}).get('language'))
             and (t.get('properties') or {}).get('codec_id') in allowed
             and not is_sub_junk(t)]
    if not valid:
        return None
    srt = [t for t in valid if (t.get('properties') or {}).get('codec_id') == 'S_TEXT/UTF8']
    return srt[0] if srt else valid[0]


def res_class(dims):
    """Standard resolution class from 'WxH' pixel dimensions.

    Classify by width first: cropped scope releases (3840x1608, 1920x800)
    must read 2160/1080, not their raw pixel height.
    """
    try:
        w, h = (int(x) for x in dims.lower().split('x'))
    except (ValueError, AttributeError):
        return 'unknown'
    if w >= 3200 or h >= 1600: return '2160'
    if w >= 1800 or h >= 900:  return '1080'
    if w >= 1100 or h >= 600:  return '720'
    return '480'


def enhanced_episode_name(stem, video_track, audio_track, ext='.mkv'):
    """stem: filename without extension, e.g. 'Show_S01E01_Title'."""
    v_props = (video_track or {}).get('properties') or {}
    height = res_class(v_props.get('pixel_dimensions') or '')

    v_codec = track_codec_short(video_track or {})
    a_codec = track_codec_short(audio_track or {})

    return f"{stem}_[{height}p_{v_codec}_{a_codec}]{ext}"


def _cleanup_partial(dst_mkv):
    # A timed-out or fatally-failed mkvmerge leaves a truncated output. Remove
    # it so the resume-skip in remux_episode (getsize > 0) re-does the job
    # instead of accepting the partial file as a finished episode.
    if dst_mkv and os.path.isfile(dst_mkv):
        try:
            os.remove(dst_mkv)
            log(f"    [CLEANUP] Removed partial output: {os.path.basename(dst_mkv)}")
        except OSError as e:
            log(f"    ⚠ Could not remove partial output {dst_mkv}: {e}")


def _episode_sidecars(mkv_path):
    """Sidecars the batch stage writes next to each episode, keyed off its stem."""
    stem = os.path.splitext(mkv_path)[0]
    return [('.nfo', stem + '.nfo'), ('-thumb.jpg', stem + '-thumb.jpg')]


def _carry_sidecars(src_mkv, dst_mkv, move=False):
    """Copy (or move) each existing episode sidecar onto the output stem.

    The remux renames the episode (adds the [2160p_hevc_eac3] suffix), so the
    sidecars have to be renamed with it or Jellyfin stops matching them.
    """
    dst_stem = os.path.splitext(dst_mkv)[0]
    for suffix, src_side in _episode_sidecars(src_mkv):
        if not os.path.isfile(src_side):
            continue
        dst_side = dst_stem + suffix
        if move:
            shutil.move(src_side, dst_side)
        elif not os.path.isfile(dst_side):
            shutil.copy2(src_side, dst_side)


def _move_episode_to_review(src_mkv, show_base, season_dir):
    fname = os.path.basename(src_mkv)
    review_dir = os.path.join(REVIEW_DIR, show_base, season_dir)
    review_target = os.path.join(review_dir, fname)
    try:
        os.makedirs(review_dir, exist_ok=True)
        if os.path.exists(review_target):
            log(f"  [REVIEW] Target exists: {review_target} — leaving source in place")
            return
        shutil.move(src_mkv, review_target)
        _carry_sidecars(src_mkv, review_target, move=True)
        log(f"  [REVIEW] Moved to: {review_target}")
    except Exception as e:
        log(f"❌ Could not move to review: {e}")


def _container_duration_s(info):
    try:
        ns = ((info.get('container') or {}).get('properties') or {}).get('duration')
        return ns / 1e9 if ns else None
    except Exception:
        return None


def _resume_output_ok(dst_mkv, src_info):
    """Validate a prior run's output before resume-skipping it.

    mkvmerge must read it without warnings, and if both durations are known
    the output must run as long as the source (remux preserves duration).
    """
    try:
        dst_info = mkv_identify(dst_mkv)
    except Exception:
        return False
    src_dur = _container_duration_s(src_info)
    dst_dur = _container_duration_s(dst_info)
    if src_dur and dst_dur and abs(src_dur - dst_dur) > 5.0:
        return False
    return True


def remux_episode(job):
    """job: dict with src_mkv, dst_dir, show_base, season_dir, tagged_folder."""
    if shutdown_requested:
        return ('skipped', job, 'shutdown')

    src_mkv = job['src_mkv']
    dst_dir = job['dst_dir']
    show_base = job['show_base']
    season_dir = job['season_dir']
    fname = os.path.basename(src_mkv)
    stem, _ = os.path.splitext(fname)
    t0 = time.perf_counter()
    log(f"  ▶ Remuxing: {show_base}/{season_dir}/{fname}")

    dst_mkv = None
    try:
        info = mkv_identify(src_mkv)
        tracks = info.get('tracks') or []
        if not tracks:
            raise Exception("No tracks in MKV")

        video_tracks = [t for t in tracks if t['type'] == 'video']
        cover_art = [t for t in video_tracks if is_video_cover_art(t)]
        if cover_art:
            log(f"  [VIDEO] Dropping {len(cover_art)} cover-art video track(s) in {fname}")
            video_tracks = [t for t in video_tracks if t not in cover_art]
        if len(video_tracks) > 1:
            log(f"  [REVIEW] {len(video_tracks)} video tracks in {fname} — needs human review")
            _move_episode_to_review(src_mkv, show_base, season_dir)
            return ('review', job, None)
        if not video_tracks:
            raise Exception("No video tracks")

        video = video_tracks[0]
        audio = pick_best_audio(tracks)
        # No acceptable audio (e.g. commentary-only): without an explicit
        # --audio-tracks selector mkvmerge would copy EVERY audio track, so
        # this must go to a human, not through the remux.
        if audio is None:
            log(f"    [REVIEW] No acceptable audio track in {fname} — needs human review")
            _move_episode_to_review(src_mkv, show_base, season_dir)
            return ('review', job, None)
        subtitle = pick_best_subtitle(tracks)

        dst_name = enhanced_episode_name(stem, video, audio, '.mkv')
        dst_mkv = os.path.join(dst_dir, dst_name)

        # Resume support: skip only if the existing output survives validation.
        # A hard kill (SIGKILL, power loss) can leave a truncated output that
        # "exists and is non-empty" — require that mkvmerge can read it cleanly
        # and that its duration matches the source before trusting it.
        if os.path.isfile(dst_mkv) and os.path.getsize(dst_mkv) > 0:
            if _resume_output_ok(dst_mkv, info):
                log(f"    [SKIP] Output exists and validates: {dst_name}")
                _carry_sidecars(src_mkv, dst_mkv)
                return ('ok', job, None)
            log(f"    [RESUME] Existing output failed validation (truncated?); "
                f"redoing: {dst_name}")
            os.remove(dst_mkv)

        os.makedirs(dst_dir, exist_ok=True)
        log(f"    [NAME] {dst_name}")

        video_id = str(video['id'])
        audio_id = str(audio['id']) if audio else None
        subtitle_id = str(subtitle['id']) if subtitle else None

        # Chapters are KEPT here, unlike the movie remux: on episodic content the
        # markers drive intro/recap skip in Jellyfin and Infuse. verify_movies.py
        # (which fails surviving chapters) never scans cleaned_tv, so this does
        # not produce flags there.
        cmd = ['mkvmerge', '-o', dst_mkv, '--no-attachments',
               '--video-tracks', video_id]
        if audio_id is not None:
            cmd += ['--audio-tracks', audio_id]
            # Relabel to eng only when the track is English/und (und on an
            # English-original show is assumed English). The non-English
            # fallback keeps its real language tag — forcing eng would ship
            # e.g. a Japanese track asserting it's English.
            if is_eng_lang((audio.get('properties') or {}).get('language')):
                cmd += ['--language', f'{audio_id}:eng']
        if subtitle_id is not None:
            cmd += ['--subtitle-tracks', subtitle_id,
                    '--language', f'{subtitle_id}:eng',
                    '--default-track-flag', f'{subtitle_id}:0']
        else:
            cmd += ['--no-subtitles']
        cmd += [src_mkv]

        # 3600s matches the movie remux: benign mkvmerge warnings (exit 1) keep
        # the output; only exit >=2 is treated as a real failure.
        run_mkvmerge(cmd, fname, timeout=3600)

        _carry_sidecars(src_mkv, dst_mkv)

        log(f"    ✔ [DONE] {fname} in {time.perf_counter()-t0:.2f}s")
        return ('ok', job, None)

    except subprocess.CalledProcessError as e:
        # mkvmerge writes diagnostics to stdout (e.output), not stderr.
        detail = ((e.output or '') + (e.stderr or '')).strip()[:500]
        log(f"    ❌ mkvmerge failed on {fname}: {detail}")
        _cleanup_partial(dst_mkv)
        return ('failed', job, detail)
    except Exception as e:
        # Covers TimeoutExpired and any other error. Drop a partial output so
        # the resume-skip (getsize > 0) doesn't later accept it as complete.
        log(f"    ❌ ERROR on {fname}: {e}")
        _cleanup_partial(dst_mkv)
        return ('failed', job, str(e))


def collect_jobs(tagged_folder):
    """Return list of episode jobs for a single show folder."""
    base = os.path.basename(tagged_folder)
    dst_show = os.path.join(CLEANED_DIR, base)
    jobs = []
    for entry in sorted(os.listdir(tagged_folder)):
        entry_path = os.path.join(tagged_folder, entry)
        if not os.path.isdir(entry_path):
            continue
        for f in sorted(os.listdir(entry_path)):
            if f.lower().endswith('.mkv'):
                jobs.append({
                    'src_mkv': os.path.join(entry_path, f),
                    'dst_dir': os.path.join(dst_show, entry),
                    'show_base': base,
                    'season_dir': entry,
                    'tagged_folder': tagged_folder,
                })
    return jobs


def finalize_show(tagged_folder, ok_count, total):
    base = os.path.basename(tagged_folder)
    dst_show = os.path.join(CLEANED_DIR, base)
    if not os.path.isdir(dst_show):
        log(f"  [FINALIZE] {base}: no output dir, skipping")
        return

    sidecars = ['tvshow.nfo', 'metadata.json', 'poster.jpg', 'fanart.jpg', 'logo.png']
    sidecars += [f for f in sorted(os.listdir(tagged_folder))
                 if f.startswith('season') and f.endswith('-poster.jpg')]
    for fname in sidecars:
        src_file = os.path.join(tagged_folder, fname)
        if os.path.isfile(src_file):
            shutil.copy2(src_file, os.path.join(dst_show, fname))

    log(f"✔ [SHOW DONE] {base}: {ok_count}/{total} episodes")
    if ok_count == total:
        try:
            shutil.rmtree(tagged_folder)
            log(f"  [CLEANUP] Removed source: {tagged_folder}")
        except Exception as e:
            log(f"❌ [CLEANUP] Failed to delete tagged folder: {e}")
    else:
        log(f"  [CLEANUP] Skipping deletion: {ok_count}/{total} succeeded")


def main():
    global shutdown_requested

    for d in (LOG_DIR, CLEANED_DIR, REVIEW_DIR):
        os.makedirs(d, exist_ok=True)

    folders = [os.path.join(TAGGED_DIR, d) for d in os.listdir(TAGGED_DIR)
               if os.path.isdir(os.path.join(TAGGED_DIR, d)) and not d.startswith('.')]

    all_jobs = []
    show_totals = {}
    for tf in folders:
        jobs = collect_jobs(tf)
        show_totals[tf] = len(jobs)
        all_jobs.extend(jobs)

    log(f"▶ Starting cleanroom remux: {len(folders)} shows, "
        f"{len(all_jobs)} episodes, max {MAX_WORKERS} concurrent")

    show_ok_counts = defaultdict(int)
    failed_eps = []
    aborted = False
    if all_jobs:
        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = [pool.submit(remux_episode, job) for job in all_jobs]
                completed = 0
                try:
                    for fut in as_completed(futures):
                        completed += 1
                        if shutdown_requested:
                            log("⚠️ Shutdown requested, cancelling remaining tasks...")
                            for f in futures:
                                f.cancel()
                            pool.shutdown(wait=False)
                            aborted = True
                            break
                        try:
                            status, job, err = fut.result(timeout=3600)
                            if status == 'ok':
                                show_ok_counts[job['tagged_folder']] += 1
                            elif status == 'failed':
                                failed_eps.append((job['src_mkv'], err))
                            log(f"📊 Progress: {completed}/{len(futures)} episodes")
                        except Exception as e:
                            log(f"❌ Worker thread error: {e}")
                            log(f"❌ Worker thread traceback: {traceback.format_exc()}")
                except KeyboardInterrupt:
                    log("⚠️ Received interrupt signal, shutting down gracefully...")
                    shutdown_requested = True
                    for f in futures:
                        f.cancel()
                    pool.shutdown(wait=False)
                    aborted = True
        except KeyboardInterrupt:
            log("⚠️ Received interrupt signal during startup, exiting...")
            return

    # Per-show finalize: copy sidecars, delete tagged source if all eps succeeded
    if not aborted:
        for tf in folders:
            finalize_show(tf, show_ok_counts.get(tf, 0), show_totals.get(tf, 0))

    if failed_eps:
        log(f"⚠️ {len(failed_eps)} episode(s) failed and remain in tagged_tv/ — rerun to retry:")
        for src, err in failed_eps:
            log(f"   {os.path.basename(src)}: {err}")

    log("✅ All TV remux operations completed.")


if __name__ == "__main__":
    main()
