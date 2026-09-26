#!/usr/bin/env python3

import os, sys, shutil, subprocess, json, time, traceback, re, signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
TAGGED_DIR = os.path.join(SERVARR_DIR, 'tagged')
CLEANED_DIR = os.path.join(SERVARR_DIR, 'cleaned')
REVIEW_DIR = os.path.join(SERVARR_DIR, 'review')
FAILED_DIR = os.path.join(SERVARR_DIR, 'failed')
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
    with open(os.path.join(LOG_DIR, 'remux_cleanroom_debug.log'), 'a') as f:
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
    # Prefer English, non-junk; fall back to any non-junk.
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

def _normalize_title_for_path(title):
    safe = re.sub(r'[\\/:*?"<>|]', '', title)
    safe = safe.replace(' ', '_').replace('.', '').strip()
    safe = re.sub(r'_+', '_', safe).strip('_')
    return safe

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

def enhanced_file_name(meta, video_track, audio_track, ext='.mkv'):
    title = meta.get('title', 'Unknown')
    year = (meta.get('release_date') or '')[:4]
    safe_title = _normalize_title_for_path(title)

    v_props = (video_track or {}).get('properties') or {}
    height = res_class(v_props.get('pixel_dimensions') or '')

    v_codec = track_codec_short(video_track or {})
    a_codec = track_codec_short(audio_track or {})

    if year:
        return f"{safe_title}_({year})_[{height}p_{v_codec}_{a_codec}]{ext}"
    return f"{safe_title}_[{height}p_{v_codec}_{a_codec}]{ext}"

def _move_to_failed(folder, base):
    failed = os.path.join(FAILED_DIR, base)
    try:
        if os.path.exists(failed):
            shutil.rmtree(failed, ignore_errors=True)
        shutil.move(folder, failed)
        log(f"  [FAILED] Moved to failed directory: {failed}")
    except Exception as e:
        log(f"❌ Could not move to failed: {e}")

def _move_to_review(folder, base):
    review = os.path.join(REVIEW_DIR, base)
    try:
        if os.path.exists(review):
            log(f"  [REVIEW] Target already exists: {review} — leaving source in place")
            return
        shutil.move(folder, review)
        log(f"  [REVIEW] Moved to review directory: {review}")
    except Exception as e:
        log(f"❌ Could not move to review: {e}")

def remux_folder(tagged_folder):
    global shutdown_requested
    if shutdown_requested:
        return

    base = os.path.basename(tagged_folder)
    log(f"\n▶ Remuxing: {base}")
    dst_folder = None
    try:
        t0 = time.perf_counter()

        mkvs = [f for f in os.listdir(tagged_folder) if f.lower().endswith('.mkv')]
        if not mkvs:
            raise Exception(f"No MKV file found in {tagged_folder}")
        if len(mkvs) > 1:
            log(f"[REVIEW] {len(mkvs)} MKVs in {base} — needs human review")
            _move_to_review(tagged_folder, base)
            return

        mkv_file = mkvs[0]
        src_mkv = os.path.join(tagged_folder, mkv_file)

        info = mkv_identify(src_mkv)
        tracks = info.get('tracks') or []
        if not tracks:
            raise Exception("No tracks found in MKV")

        video_tracks = [t for t in tracks if t['type'] == 'video']
        if len(video_tracks) > 1:
            log(f"[REVIEW] {len(video_tracks)} video tracks in {base} — needs human review")
            _move_to_review(tagged_folder, base)
            return
        if not video_tracks:
            raise Exception("No video tracks in MKV")

        video = video_tracks[0]
        audio = pick_best_audio(tracks)
        # No acceptable audio (e.g. commentary-only): without an explicit
        # --audio-tracks selector mkvmerge would copy EVERY audio track, so
        # this must go to a human, not through the remux.
        if audio is None:
            log(f"[REVIEW] No acceptable audio track in {base} — needs human review")
            _move_to_review(tagged_folder, base)
            return
        subtitle = pick_best_subtitle(tracks)

        # Read metadata.json (written by batch_cleaner) for the enhanced filename
        meta = {}
        metadata_file = os.path.join(tagged_folder, "metadata.json")
        if os.path.exists(metadata_file):
            try:
                with open(metadata_file, 'r') as f:
                    meta = json.load(f)
            except Exception as e:
                log(f"  [WARN] Could not read metadata.json: {e}")

        dst_folder = os.path.join(CLEANED_DIR, base)
        os.makedirs(dst_folder, exist_ok=True)
        if meta:
            dst_mkv_name = enhanced_file_name(meta, video, audio, '.mkv')
        else:
            dst_mkv_name = mkv_file
        dst_mkv = os.path.join(dst_folder, dst_mkv_name)
        log(f"  [NAME] Output: {dst_mkv_name}")

        video_id = str(video['id'])
        audio_id = str(audio['id']) if audio else None
        subtitle_id = str(subtitle['id']) if subtitle else None

        cmd = ['mkvmerge', '-o', dst_mkv, '--no-chapters', '--no-attachments',
               '--video-tracks', video_id]
        if audio_id is not None:
            cmd += ['--audio-tracks', audio_id]
            # Relabel to eng only when the track is English/und (und on an
            # English-original film is assumed English). The non-English
            # fallback keeps its real language tag — forcing eng would ship
            # e.g. a French dub asserting it's English.
            if is_eng_lang((audio.get('properties') or {}).get('language')):
                cmd += ['--language', f'{audio_id}:eng']
        if subtitle_id is not None:
            cmd += ['--subtitle-tracks', subtitle_id,
                    '--language', f'{subtitle_id}:eng',
                    '--default-track-flag', f'{subtitle_id}:0']
        else:
            cmd += ['--no-subtitles']
        cmd += [src_mkv]

        log(f"  [CMD] {' '.join(cmd)}")
        # 3600s: largest ~90 GB 4K remuxes run ~2600s at the measured ~34 MB/s
        # floor under 4-way + co-resident Jellyfin load. 1800s was on the edge
        # (60 GB files finished at 1798s).
        run_mkvmerge(cmd, base, timeout=3600)

        # Copy sidecars
        for fname in ['movie.nfo', 'poster.jpg', 'fanart.jpg']:
            src_file = os.path.join(tagged_folder, fname)
            if os.path.isfile(src_file):
                shutil.copy2(src_file, os.path.join(dst_folder, fname))

        log(f"✔ [DONE] Remuxed {base} in {time.perf_counter()-t0:.2f}s")

        # Delete the tagged source after successful remux
        try:
            shutil.rmtree(tagged_folder)
        except Exception as cleanup_error:
            log(f"❌ [CLEANUP] Failed to delete tagged folder: {cleanup_error}")
            log(f"  [CLEANUP] Remux successful but manual cleanup needed for: {tagged_folder}")

    except subprocess.CalledProcessError as e:
        detail = ((e.output or '') + (e.stderr or '')).strip()[:500]
        log(f"❌ mkvmerge failed on {base}: {detail}")
        if dst_folder and os.path.isdir(dst_folder):
            shutil.rmtree(dst_folder, ignore_errors=True)
        if os.path.isdir(tagged_folder):
            _move_to_failed(tagged_folder, base)
    except Exception as e:
        log(f"❌ ERROR on {base}: {e}\n{traceback.format_exc()}")
        if dst_folder and os.path.isdir(dst_folder):
            shutil.rmtree(dst_folder, ignore_errors=True)
        if os.path.isdir(tagged_folder):
            _move_to_failed(tagged_folder, base)

def main():
    global shutdown_requested

    for d in (LOG_DIR, CLEANED_DIR, REVIEW_DIR, FAILED_DIR, TAGGED_DIR):
        os.makedirs(d, exist_ok=True)

    tagged_folders = [os.path.join(TAGGED_DIR, d) for d in os.listdir(TAGGED_DIR)
                      if os.path.isdir(os.path.join(TAGGED_DIR, d)) and not d.startswith('.')]
    total = len(tagged_folders)
    log(f"▶ Starting cleanroom remux: {len(tagged_folders)} tagged "
        f"(max {MAX_WORKERS} threads)")

    if total:
        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = [pool.submit(remux_folder, f) for f in tagged_folders]
                completed = 0

                try:
                    for fut in as_completed(futures):
                        completed += 1
                        log(f"📊 Progress: {completed}/{total} folders completed")

                        if shutdown_requested:
                            log("⚠️ Shutdown requested, cancelling remaining tasks...")
                            for f in futures:
                                f.cancel()
                            pool.shutdown(wait=False)
                            log("⚠️ Shutdown complete. Some folders may not have been processed.")
                            return

                        try:
                            fut.result(timeout=1800)
                        except Exception as e:
                            log(f"❌ Worker thread error: {e}")
                            log(f"❌ Worker thread traceback: {traceback.format_exc()}")

                except KeyboardInterrupt:
                    log("⚠️ Received interrupt signal, shutting down gracefully...")
                    shutdown_requested = True
                    for f in futures:
                        f.cancel()
                    pool.shutdown(wait=False)
                    log("⚠️ Shutdown complete. Some folders may not have been processed.")
                    return

        except KeyboardInterrupt:
            log("⚠️ Received interrupt signal during startup, exiting...")
            return

    log("✅ All remux operations completed.")


if __name__ == "__main__":
    main()
