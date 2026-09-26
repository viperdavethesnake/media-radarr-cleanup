#!/usr/bin/env python3
"""
Compare two movie libraries and recommend which copy to keep.

PathA (new copies):  $RC_SERVARR_DIR/cleaned (default /storage/media/servarr/cleaned)
PathB (older copies): /storage/media/movies,/storage/media/movies2 (default)

PathB accepts multiple comma-separated roots, since the cold-storage migration
split older titles across two datasets (movies + movies2). All roots are merged
into a single EXISTING index for matching; --apply replaces a matched movie in
place in whichever root it was actually found in.

Either or both paths may be remote — pass --ssh-a user@host or --ssh-b user@host
to run filesystem ops and ffprobe/mkvmerge over SSH for that side (same SSH host
is used for every comma-separated path-b root).

Default run is read-only: prints a side-by-side summary and writes a JSON
plan to $RC_SERVARR_DIR/logs/compare_plan_<ts>.json.

With --apply, HIGH-confidence, imdb-keyed recommendations are executed:
  KEEP NEW      -> fix_media_perms on the new folder, then replace the
                   existing folder with the new one.
  KEEP EXISTING -> delete the new folder.
Anything else (TIE, lower confidence, weak key) is skipped.

Requires external tools (locally or on the SSH host):
- ffprobe (from FFmpeg)
- mkvmerge, mkvextract (from MKVToolNix)
"""

from __future__ import annotations

import argparse
import atexit
import concurrent.futures as cf
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET
from dotenv import load_dotenv

load_dotenv()


# NEW = newly downloaded / newly processed copies (candidate replacements)
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
DEFAULT_NEW_PATH = os.path.join(SERVARR_DIR, 'cleaned')
LOG_DIR = os.path.join(SERVARR_DIR, 'logs')

# EXISTING = your current library (the stuff you already have).
# Comma-separated: split across two datasets since the cold-storage migration
# moved older titles out to movies2.
DEFAULT_EXISTING_PATH = "/storage/media/movies,/storage/media/movies2"

SIDE_NEW = "NEW"
SIDE_EXISTING = "EXISTING"


# ---------------------------------------------------------------------------
# Shell abstraction (local or SSH)
# ---------------------------------------------------------------------------

class Shell:
    """Abstracts local-filesystem and SSH-remote operations uniformly.

    host=None  → run everything locally
    host="user@ip" or host="ip"  → run via SSH (BatchMode, no password prompts)

    Tip: set up ~/.ssh/config with ControlMaster/ControlPersist to avoid
    per-connection handshake overhead when probing hundreds of movies.
    """

    def __init__(self, host: Optional[str] = None) -> None:
        self.host = host
        self._cm_sock: Optional[str] = None
        if host:
            self._cm_sock = os.path.join(
                tempfile.gettempdir(),
                f"ssh_cm_{self.host.replace('@', '_').replace('.', '_')}.sock",
            )
            atexit.register(self._stop_control_master)

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _stop_control_master(self) -> None:
        if self._cm_sock and self.host:
            subprocess.run(
                ["ssh", "-o", f"ControlPath={self._cm_sock}", "-O", "exit", self.host],
                capture_output=True,
                timeout=5,
            )

    def _ssh(self, remote_cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
        # ControlMaster=auto: first call creates the shared socket, rest reuse it.
        # ControlPersist keeps it alive after the first call exits.
        args = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", f"ControlPath={self._cm_sock}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=300s",
            self.host,
            remote_cmd,
        ]
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    # ------------------------------------------------------------------ #
    # Filesystem helpers
    # ------------------------------------------------------------------ #

    def list_subdirs(self, path: str) -> List[str]:
        """Sorted list of immediate subdirectory full-paths (symlinks skipped)."""
        if not self.host:
            try:
                entries = os.listdir(path)
            except FileNotFoundError:
                return []
            out: List[str] = []
            for e in entries:
                p = os.path.join(path, e)
                if os.path.isdir(p) and not os.path.islink(p):
                    out.append(p)
            return sorted(out)
        # Remote: trailing slash on ls -d confirms directory; strip it back
        r = self._ssh(f"ls -1d {shlex.quote(path)}/*/  2>/dev/null; true")
        return sorted(line.rstrip("/") for line in r.stdout.splitlines() if line.strip())

    def list_files(self, path: str) -> List[str]:
        """Filenames (not full paths) of regular files directly inside path."""
        if not self.host:
            try:
                entries = os.listdir(path)
                return [e for e in entries if os.path.isfile(os.path.join(path, e))]
            except Exception:
                return []
        r = self._ssh(f"ls -1 {shlex.quote(path)} 2>/dev/null")
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    def read_text(self, path: str, max_bytes: int = 2_000_000) -> Optional[str]:
        if not self.host:
            try:
                with open(path, "rb") as f:
                    return f.read(max_bytes).decode("utf-8", errors="ignore")
            except Exception:
                return None
        r = self._ssh(f"cat {shlex.quote(path)} 2>/dev/null")
        return r.stdout[:max_bytes] if r.stdout else None

    def getsize(self, path: str) -> Optional[int]:
        if not self.host:
            try:
                return os.path.getsize(path)
            except Exception:
                return None
        r = self._ssh(f"stat -c %s {shlex.quote(path)} 2>/dev/null")
        return _safe_int(r.stdout.strip())

    def which_ok(self, bin_name: str) -> bool:
        if not self.host:
            from shutil import which
            return which(bin_name) is not None
        r = self._ssh(f"which {shlex.quote(bin_name)} >/dev/null 2>&1 && echo yes || echo no")
        return r.stdout.strip() == "yes"

    # ------------------------------------------------------------------ #
    # Command execution (returns parsed JSON)
    # ------------------------------------------------------------------ #

    def run_json(self, cmd: List[str], timeout: int) -> Dict[str, Any]:
        if not self.host:
            return _run_json(cmd, timeout)
        remote_cmd = " ".join(shlex.quote(c) for c in cmd)
        r = self._ssh(remote_cmd, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(
                f"Remote command failed ({r.returncode}): {remote_cmd}\n{r.stderr.strip()}"
            )
        try:
            return json.loads(r.stdout)
        except Exception as e:
            raise RuntimeError(f"Invalid JSON from remote: {remote_cmd}: {e}") from e


# ---------------------------------------------------------------------------
# Low-level utilities
# ---------------------------------------------------------------------------

def _run_json(cmd: List[str], timeout: int) -> Dict[str, Any]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr.strip()}")
    try:
        return json.loads(p.stdout)
    except Exception as e:
        raise RuntimeError(f"Invalid JSON from: {' '.join(cmd)}: {e}") from e


def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


def _norm_title(s: str) -> str:
    s = s.replace("_", " ").strip().lower()
    s = re.sub(r"\[[^\]]+\]", " ", s)  # drop [2160p_HEVC_EAC3] style chunks
    s = re.sub(r"\([^)]*\)", " ", s)  # drop (...) chunks (often year)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_imdb_any(text: str) -> Optional[str]:
    m = re.search(r"(tt\d{6,9})", text, re.IGNORECASE)
    return m.group(1).lower() if m else None


def _read_mkv_imdb(mkv_path: str, shell: Shell) -> Optional[str]:
    """Read IMDb ID from embedded MKV tags — authoritative per the project data contract."""
    try:
        if shell.host:
            r = shell._ssh(f"mkvextract tags {shlex.quote(mkv_path)}", timeout=30)
            stdout = r.stdout if r.returncode == 0 else ""
        else:
            p = subprocess.run(
                ["mkvextract", "tags", mkv_path],
                capture_output=True, text=True, timeout=30,
            )
            stdout = p.stdout if p.returncode == 0 else ""
    except Exception:
        return None
    if not stdout.strip():
        return None
    try:
        root = ET.fromstring(stdout)
    except Exception:
        return None
    for simple in root.iter("Simple"):
        name = (simple.findtext("Name") or "").strip().upper()
        if name != "IMDB":
            continue
        value = (simple.findtext("String") or "").strip()
        m = re.search(r"tt\d{6,9}", value, re.IGNORECASE)
        if m:
            return m.group(0).lower()
    return None


def _pick_main_mkv(folder: str, shell: Shell) -> Tuple[Optional[str], List[str]]:
    mkvs = [f for f in shell.list_files(folder) if f.lower().endswith(".mkv")]
    if not mkvs:
        return None, []
    if len(mkvs) == 1:
        return folder + "/" + mkvs[0], mkvs
    # Per workflow requirement: ignore folders with multiple MKVs (ambiguous).
    return None, mkvs


def _parse_movie_nfo(folder: str, shell: Shell) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (imdb_id, title, year)"""
    nfo_path = folder + "/movie.nfo"
    txt = shell.read_text(nfo_path)
    if not txt:
        # Try any .nfo in the folder
        for f in shell.list_files(folder):
            if f.lower().endswith(".nfo"):
                txt = shell.read_text(folder + "/" + f)
                if txt:
                    break
    if not txt:
        return None, None, None

    imdb: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None

    # XML parse first (best effort)
    try:
        root = ET.fromstring(txt)
        imdb_raw = root.findtext("imdbid") or root.findtext("id")
        if imdb_raw:
            imdb = _extract_imdb_any(imdb_raw)
        title_raw = root.findtext("title")
        if title_raw:
            title = title_raw.strip()
        year_raw = root.findtext("year")
        if year_raw and re.fullmatch(r"\d{4}", year_raw.strip()):
            year = year_raw.strip()
    except Exception:
        pass

    # Regex fallback for imdb + year/title
    if not imdb:
        imdb = _extract_imdb_any(txt)
    if not year:
        m = re.search(r"<year>\s*(\d{4})\s*</year>", txt, re.IGNORECASE)
        if m:
            year = m.group(1)
    if not title:
        m = re.search(r"<title>\s*(.*?)\s*</title>", txt, re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()

    return imdb, title, year


def _infer_title_year_from_folder(folder: str) -> Tuple[Optional[str], Optional[str]]:
    base = os.path.basename(folder)
    m = re.search(r"(.+?)\s*\(?(\d{4})\)?", base)
    if not m:
        return None, None
    raw_title = m.group(1)
    raw_year = m.group(2)
    title = raw_title.replace("_", " ").strip()
    year = raw_year if re.fullmatch(r"\d{4}", raw_year) else None
    return title or None, year


def _make_key(imdb: Optional[str], title: Optional[str], year: Optional[str], folder: str) -> Tuple[str, str]:
    if imdb:
        return "imdb", imdb.lower()
    if title and year:
        return "titleyear", f"{_norm_title(title)}::{year}"
    # Last resort: normalized folder name (more collisions; flagged later)
    return "folder", _norm_title(os.path.basename(folder))


def _human_bytes(n: Optional[int]) -> str:
    if n is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f"{f:.1f}{u}" if u != "B" else f"{int(f)}B"
        f /= 1024.0
    return f"{int(n)}B"


def _human_bps(bps: Optional[int]) -> str:
    if bps is None:
        return "?"
    if bps >= 1_000_000:
        return f"{bps/1_000_000:.2f}Mb/s"
    if bps >= 1_000:
        return f"{bps/1_000:.0f}kb/s"
    return f"{bps}b/s"


def _parse_ratio(r: Any) -> Optional[float]:
    # ffprobe often returns strings like "24000/1001"
    if r is None:
        return None
    if isinstance(r, (int, float)):
        return float(r)
    if isinstance(r, str):
        if "/" in r:
            a, b = r.split("/", 1)
            fa = _safe_float(a)
            fb = _safe_float(b)
            if fa is not None and fb not in (None, 0.0):
                return fa / fb
        return _safe_float(r)
    return None


def _codec_rank(codec: str) -> int:
    c = (codec or "").lower()
    # Lower is better.
    if "truehd" in c:
        return 0
    if "dts" in c and ("hd" in c or "ma" in c):
        return 1
    if "dts" in c:
        return 2
    if "eac3" in c or "ec-3" in c:
        return 3
    if "ac3" in c:
        return 4
    if "flac" in c:
        return 5
    if "aac" in c:
        return 6
    if "mp3" in c:
        return 7
    return 99


def _is_english(lang: Optional[str]) -> bool:
    if not lang:
        return True  # treat missing as acceptable
    lang = lang.lower()
    return lang in {"en", "eng", "en-us", "en-gb", "und", ""}


@dataclass(frozen=True)
class TrackSummary:
    idx: int
    lang: str
    codec: str
    codec_long: str
    channels: str
    sample_rate_hz: Optional[int]
    bitrate_bps: Optional[int]
    title: str
    default: bool
    atmos: bool


@dataclass(frozen=True)
class MediaSummary:
    mkv_path: str
    size_bytes: Optional[int]
    duration_s: Optional[float]
    # Video
    v_codec: str
    v_profile: str
    v_level: str
    v_res: str
    v_fps: Optional[float]
    v_bit_depth: Optional[int]
    v_pix_fmt: str
    v_bitrate_bps: Optional[int]
    v_hdr_format: str  # DV / HDR10+ / HDR10 / HLG / SDR / ?
    v_color_primaries: str
    v_color_transfer: str
    v_colorspace: str
    v_mastering: str
    v_maxcll: Optional[int]
    v_maxfall: Optional[int]
    # Audio
    audio_tracks: Tuple[TrackSummary, ...]
    best_audio_idx: Optional[int]


def _ffprobe(mkv_path: str, shell: Shell) -> Dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        mkv_path,
    ]
    return shell.run_json(cmd, timeout=60)


def _mkvmerge_json(mkv_path: str, shell: Shell) -> Dict[str, Any]:
    return shell.run_json(["mkvmerge", "-J", mkv_path], timeout=60)


def _bit_depth_from_pix_fmt(pix_fmt: str) -> Optional[int]:
    # Common: yuv420p10le, yuv444p12le, etc.
    m = re.search(r"p(\d{2})", pix_fmt or "")
    if m:
        return int(m.group(1))
    return None


def _pick_default_stream(streams: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not streams:
        return None
    streams = list(streams)
    streams.sort(key=lambda s: 0 if (s.get("disposition") or {}).get("default") == 1 else 1)
    return streams[0]


def _detect_hdr_format(v: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Returns (hdr_format, mastering_sd, cll_sd)"""
    side = v.get("side_data_list") or []
    side_types = [str(sd.get("side_data_type") or "") for sd in side]
    side_types_l = [t.lower() for t in side_types]

    mastering = None
    cll = None
    dovi_seen = False
    for sd in side:
        t = str(sd.get("side_data_type") or "")
        tl = t.lower()
        if "mastering display metadata" in tl:
            mastering = sd
        if "content light level" in tl:
            cll = sd
        if "dovi configuration record" in tl or tl.strip() == "dovi":
            dovi_seen = True

    if dovi_seen:
        return "DV", mastering, cll

    if any("hdr10+" in tl or "2094-40" in tl or "hdr10+ metadata" in tl for tl in side_types_l):
        return "HDR10+", mastering, cll

    tr = (v.get("color_transfer") or "").lower()
    cp = (v.get("color_primaries") or "").lower()

    if tr == "arib-std-b67":
        return "HLG", mastering, cll

    if tr == "smpte2084" and cp.startswith("bt2020"):
        return "HDR10", mastering, cll

    if mastering is not None or cll is not None:
        return "UNKNOWN", mastering, cll

    return "SDR", mastering, cll


def _fmt_mastering(mastering_sd: Optional[Dict[str, Any]]) -> str:
    if not mastering_sd:
        return ""
    max_lum = mastering_sd.get("max_luminance")
    min_lum = mastering_sd.get("min_luminance")
    parts = []
    if max_lum is not None:
        parts.append(f"maxLum={max_lum}")
    if min_lum is not None:
        parts.append(f"minLum={min_lum}")
    return " ".join(parts)


def _extract_maxcll_maxfall(cll_sd: Optional[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
    if not cll_sd:
        return None, None
    maxcll = _safe_int(cll_sd.get("max_content"))
    maxfall = _safe_int(cll_sd.get("max_average"))
    return maxcll, maxfall


def _collect_audio_tracks(ffp: Dict[str, Any], mkv: Dict[str, Any]) -> List[TrackSummary]:
    # Map mkvmerge track id -> properties (language, track_name, flags)
    mkv_tracks = mkv.get("tracks") or []
    mkv_audio = [t for t in mkv_tracks if t.get("type") == "audio"]
    by_id: Dict[int, Dict[str, Any]] = {}
    for t in mkv_audio:
        try:
            tid = int(t.get("id"))
        except Exception:
            continue
        by_id[tid] = t.get("properties") or {}

    streams = ffp.get("streams") or []
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    out: List[TrackSummary] = []
    for idx, s in enumerate(audios):
        tags = s.get("tags") or {}
        lang = (tags.get("language") or "").lower()
        codec = (s.get("codec_name") or "").lower() or "?"
        codec_long = (s.get("codec_long_name") or "").strip()
        ch = s.get("channels")
        layout = s.get("channel_layout")
        ch_s = "?"
        if ch is not None:
            ch_s = str(ch)
            if layout:
                ch_s = f"{ch_s}ch({layout})"

        sample_rate_hz = _safe_int(s.get("sample_rate"))
        bitrate_bps = _safe_int(s.get("bit_rate"))

        title = ""
        default = False
        sid = _safe_int(s.get("index"))
        props = by_id.get(sid, {}) if sid is not None else {}
        title = (props.get("track_name") or "").strip()
        default = bool(props.get("default_track")) or bool((s.get("disposition") or {}).get("default") == 1)

        if not title:
            title = (tags.get("title") or "").strip()

        title_l = title.lower()
        atmos = False
        if "atmos" in title_l:
            atmos = True
        if codec in {"truehd", "eac3", "ec-3"} and ("joc" in title_l or "dolby atmos" in title_l):
            atmos = True

        out.append(
            TrackSummary(
                idx=idx,
                lang=lang or "und",
                codec=codec,
                codec_long=codec_long,
                channels=ch_s,
                sample_rate_hz=sample_rate_hz,
                bitrate_bps=bitrate_bps,
                title=title,
                default=default,
                atmos=atmos,
            )
        )
    return out


def _pick_best_audio(tracks: List[TrackSummary]) -> Optional[int]:
    if not tracks:
        return None

    def score(t: TrackSummary) -> Tuple[int, int, int]:
        lang_pen = 0 if _is_english(t.lang) else 1
        cr = _codec_rank(t.codec)
        def_pen = 0 if t.default else 1
        return (lang_pen, cr, def_pen)

    best = min(tracks, key=score)
    return best.idx


def summarize_media(mkv_path: str, shell: Shell) -> MediaSummary:
    size = shell.getsize(mkv_path)

    ffp = _ffprobe(mkv_path, shell)
    mkv = _mkvmerge_json(mkv_path, shell)

    dur_s: Optional[float] = None
    try:
        dur_raw = (ffp.get("format") or {}).get("duration")
        dur_s = float(dur_raw) if dur_raw is not None else None
    except Exception:
        dur_s = None

    streams = ffp.get("streams") or []
    vids = [s for s in streams if s.get("codec_type") == "video"]
    v = _pick_default_stream(vids) or {}

    v_codec = (v.get("codec_name") or "?").lower()
    v_profile = str(v.get("profile") or "").strip()
    v_level = str(v.get("level") or "").strip()
    w = v.get("width")
    h = v.get("height")
    v_res = f"{w}x{h}" if w and h else "?"
    v_pix_fmt = str(v.get("pix_fmt") or "").strip()
    v_bit_depth = _bit_depth_from_pix_fmt(v_pix_fmt)
    v_bitrate_bps = _safe_int(v.get("bit_rate"))

    v_fps = _parse_ratio(v.get("avg_frame_rate")) or _parse_ratio(v.get("r_frame_rate"))

    v_color_primaries = str(v.get("color_primaries") or "").strip()
    v_color_transfer = str(v.get("color_transfer") or "").strip()
    v_colorspace = str(v.get("colorspace") or "").strip()

    v_hdr_format, mastering_sd, cll_sd = _detect_hdr_format(v)
    v_mastering = _fmt_mastering(mastering_sd)
    v_maxcll, v_maxfall = _extract_maxcll_maxfall(cll_sd)

    audio_tracks = _collect_audio_tracks(ffp, mkv)
    best_audio_idx = _pick_best_audio(audio_tracks)

    return MediaSummary(
        mkv_path=mkv_path,
        size_bytes=size,
        duration_s=dur_s,
        v_codec=v_codec,
        v_profile=v_profile,
        v_level=v_level,
        v_res=v_res,
        v_fps=v_fps,
        v_bit_depth=v_bit_depth,
        v_pix_fmt=v_pix_fmt,
        v_bitrate_bps=v_bitrate_bps,
        v_hdr_format=v_hdr_format,
        v_color_primaries=v_color_primaries,
        v_color_transfer=v_color_transfer,
        v_colorspace=v_colorspace,
        v_mastering=v_mastering,
        v_maxcll=v_maxcll,
        v_maxfall=v_maxfall,
        audio_tracks=tuple(audio_tracks),
        best_audio_idx=best_audio_idx,
    )


@dataclass
class MovieEntry:
    folder: str
    mkv_path: Optional[str]
    mkv_names: List[str]
    imdb: Optional[str]
    title: Optional[str]
    year: Optional[str]
    key_type: str
    key_value: str
    warnings: List[str]

    @property
    def key(self) -> Tuple[str, str]:
        return (self.key_type, self.key_value)


def _index_one(folder: str, side_label: str, shell: Shell) -> Tuple[Optional[MovieEntry], Optional[str]]:
    """Worker for index_library. Returns (entry, problem_message)."""
    warnings: List[str] = []
    mkv_path, mkv_names = _pick_main_mkv(folder, shell)
    if len(mkv_names) > 1:
        return None, (
            f"[SKIP] {side_label}: multiple MKVs in folder "
            f"'{os.path.basename(folder)}' ({len(mkv_names)} files)"
        )
    if not mkv_path:
        warnings.append("no_mkv")

    # Identity priority per project data contract:
    #   1) embedded MKV tag (authoritative)
    #   2) movie.nfo
    #   3) folder / mkv filename regex
    imdb: Optional[str] = None
    if mkv_path:
        imdb = _read_mkv_imdb(mkv_path, shell)

    nfo_imdb, title, year = _parse_movie_nfo(folder, shell)
    if not imdb:
        imdb = nfo_imdb

    if not imdb:
        imdb = _extract_imdb_any(os.path.basename(folder))
        if not imdb and mkv_path:
            imdb = _extract_imdb_any(os.path.basename(mkv_path))

    if not title or not year:
        t2, y2 = _infer_title_year_from_folder(folder)
        title = title or t2
        year = year or y2

    key_type, key_value = _make_key(imdb, title, year, folder)
    if key_type != "imdb":
        warnings.append(f"weak_key({key_type})")

    ent = MovieEntry(
        folder=folder,
        mkv_path=mkv_path,
        mkv_names=mkv_names,
        imdb=imdb,
        title=title,
        year=year,
        key_type=key_type,
        key_value=key_value,
        warnings=warnings,
    )
    return ent, None


def index_library(root: str, side_label: str, shell: Shell, threads: int = 8) -> Tuple[Dict[Tuple[str, str], List[MovieEntry]], List[str]]:
    problems: List[str] = []
    by_key: Dict[Tuple[str, str], List[MovieEntry]] = {}

    folders = shell.list_subdirs(root)
    if not folders:
        problems.append(f"[WARN] {side_label}: No folders found under: {root}")
        return by_key, problems

    with cf.ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
        futs = [ex.submit(_index_one, f, side_label, shell) for f in folders]
        for fut in cf.as_completed(futs):
            ent, problem = fut.result()
            if problem:
                problems.append(problem)
            if ent:
                by_key.setdefault(ent.key, []).append(ent)

    return by_key, problems


def index_libraries(roots: List[str], side_label: str, shell: Shell, threads: int = 8) -> Tuple[Dict[Tuple[str, str], List[MovieEntry]], List[str]]:
    """Index multiple roots (e.g. movies + movies2) and merge into one index.

    A key found in more than one root ends up with >1 entries, which the
    existing ambiguous-match logic in main() already handles correctly.
    """
    merged: Dict[Tuple[str, str], List[MovieEntry]] = {}
    problems: List[str] = []
    for root in roots:
        by_key, root_problems = index_library(root, side_label, shell, threads=threads)
        problems.extend(root_problems)
        for k, entries in by_key.items():
            merged.setdefault(k, []).extend(entries)
    return merged, problems


def _fmt_identity(e: MovieEntry) -> str:
    if e.title and e.year:
        return f"{e.title} ({e.year})"
    if e.imdb:
        return e.imdb
    return os.path.basename(e.folder)


def _fmt_audio_tracks(ms: MediaSummary) -> str:
    if not ms.audio_tracks:
        return "audio: (none)"

    best: Optional[TrackSummary] = None
    for t in ms.audio_tracks:
        if ms.best_audio_idx == t.idx:
            best = t
            break
    if best is None:
        best = ms.audio_tracks[0]

    atmos = " Atmos" if best.atmos else ""
    title = f" title='{best.title}'" if best.title else ""
    sr = f" sr={best.sample_rate_hz}Hz" if best.sample_rate_hz else ""
    br = f" br={_human_bps(best.bitrate_bps)}" if best.bitrate_bps else ""
    d = " default" if best.default else ""

    others = len(ms.audio_tracks) - 1
    other_s = f" (+{others} other audio tracks)" if others > 0 else ""
    return f"audio(best): a{best.idx}:{best.lang}:{best.codec}{atmos}:{best.channels}{d}{sr}{br}{title}{other_s}"


def _fmt_video(ms: MediaSummary) -> str:
    fps = f"{ms.v_fps:.3f}fps" if ms.v_fps is not None else "?fps"
    bd = f"{ms.v_bit_depth}-bit" if ms.v_bit_depth else "?bit"
    prof = f" prof={ms.v_profile}" if ms.v_profile else ""
    lvl = f" lvl={ms.v_level}" if ms.v_level else ""
    br = f" br={_human_bps(ms.v_bitrate_bps)}" if ms.v_bitrate_bps else " br=?"
    pix = f" pix={ms.v_pix_fmt}" if ms.v_pix_fmt else ""
    return f"video: {ms.v_codec} {ms.v_res} {bd} {fps}{br}{prof}{lvl}{pix}"


def _fmt_hdr(ms: MediaSummary) -> str:
    parts = [f"hdr: {ms.v_hdr_format}"]
    if ms.v_color_primaries:
        parts.append(f"prim={ms.v_color_primaries}")
    if ms.v_color_transfer:
        parts.append(f"trc={ms.v_color_transfer}")
    if ms.v_colorspace:
        parts.append(f"cs={ms.v_colorspace}")
    if ms.v_maxcll is not None or ms.v_maxfall is not None:
        parts.append(f"MaxCLL={ms.v_maxcll if ms.v_maxcll is not None else '?'}")
        parts.append(f"MaxFALL={ms.v_maxfall if ms.v_maxfall is not None else '?'}")
    if ms.v_mastering:
        parts.append(f"mastering({ms.v_mastering})")
    return " ".join(parts)


def _parse_res_pixels(res: str) -> int:
    m = re.match(r"(\d+)x(\d+)", res or "")
    if not m:
        return 0
    return int(m.group(1)) * int(m.group(2))


def _hdr_rank(h: str) -> int:
    h = (h or "").upper()
    order = {
        "DV": 5,
        "HDR10+": 4,
        "HDR10": 3,
        "HLG": 2,
        "HDR(PQ)": 2,
        "WCG(BT2020)": 1,
        "SDR": 0,
    }
    return order.get(h, -1)


def _vcodec_rank(c: str) -> int:
    c = (c or "").lower()
    if c == "av1":
        return 4
    if c in {"hevc", "h265", "h.265"}:
        return 3
    if c in {"h264", "avc", "h.264"}:
        return 2
    if c in {"mpeg2video", "mpeg2"}:
        return 1
    return 0


def _audio_channels_count(chs: str) -> int:
    m = re.match(r"(\d+)ch", chs or "")
    if m:
        return int(m.group(1))
    return _safe_int(chs) or 0


def _best_audio(ms: MediaSummary) -> Optional[TrackSummary]:
    if not ms.audio_tracks:
        return None
    if ms.best_audio_idx is None:
        return ms.audio_tracks[0]
    for t in ms.audio_tracks:
        if t.idx == ms.best_audio_idx:
            return t
    return ms.audio_tracks[0]


def _video_bitrate_effective_bps(ms: MediaSummary) -> Tuple[Optional[int], bool]:
    """Returns (bps, used_proxy). Proxy = file_size/duration (rough but useful for comparisons)."""
    if ms.v_bitrate_bps and ms.v_bitrate_bps > 0:
        return ms.v_bitrate_bps, False
    if ms.size_bytes and ms.duration_s and ms.duration_s > 0:
        return int((ms.size_bytes * 8) / ms.duration_s), True
    return None, True


def recommend_keep(new: MediaSummary, existing: MediaSummary) -> Tuple[str, str, List[str], str]:
    """
    Returns (keep, basis, reasons, confidence)
    keep: "NEW" | "EXISTING" | "TIE"
    basis: "video" | "audio" | "tie"
    confidence: "high" | "medium" | "low"
    """
    reasons: List[str] = []
    confidence = "high"

    # 1) VIDEO-FIRST comparison (dominant)
    new_px = _parse_res_pixels(new.v_res)
    ex_px = _parse_res_pixels(existing.v_res)
    if new_px != ex_px:
        keep = SIDE_NEW if new_px > ex_px else SIDE_EXISTING
        reasons.append(f"{keep}: higher resolution ({SIDE_NEW}={new.v_res} vs {SIDE_EXISTING}={existing.v_res})")
        return keep, "video", reasons, confidence

    new_hdr = _hdr_rank(new.v_hdr_format)
    ex_hdr = _hdr_rank(existing.v_hdr_format)
    if new_hdr != ex_hdr:
        keep = SIDE_NEW if new_hdr > ex_hdr else SIDE_EXISTING
        reasons.append(f"{keep}: better HDR ({SIDE_NEW}={new.v_hdr_format} vs {SIDE_EXISTING}={existing.v_hdr_format})")
        return keep, "video", reasons, confidence

    if new.v_bit_depth and existing.v_bit_depth and new.v_bit_depth != existing.v_bit_depth:
        keep = SIDE_NEW if new.v_bit_depth > existing.v_bit_depth else SIDE_EXISTING
        reasons.append(
            f"{keep}: higher bit depth ({SIDE_NEW}={new.v_bit_depth}-bit vs {SIDE_EXISTING}={existing.v_bit_depth}-bit)"
        )
        return keep, "video", reasons, confidence
    if (new.v_bit_depth is None) != (existing.v_bit_depth is None):
        confidence = "medium"

    new_vc = _vcodec_rank(new.v_codec)
    ex_vc = _vcodec_rank(existing.v_codec)
    if new_vc != ex_vc:
        keep = SIDE_NEW if new_vc > ex_vc else SIDE_EXISTING
        reasons.append(f"{keep}: better video codec ({SIDE_NEW}={new.v_codec} vs {SIDE_EXISTING}={existing.v_codec})")
        return keep, "video", reasons, confidence

    new_vbr, new_proxy = _video_bitrate_effective_bps(new)
    ex_vbr, ex_proxy = _video_bitrate_effective_bps(existing)
    if new_proxy or ex_proxy:
        confidence = "medium" if confidence == "high" else confidence
    if new_vbr and ex_vbr:
        hi = max(new_vbr, ex_vbr)
        lo = min(new_vbr, ex_vbr)
        if lo > 0 and (hi / lo) >= 1.10:
            keep = SIDE_NEW if new_vbr > ex_vbr else SIDE_EXISTING
            suffix = " (proxy)" if (new_proxy or ex_proxy) else ""
            reasons.append(
                f"{keep}: higher effective bitrate{suffix} ({SIDE_NEW}={_human_bps(new_vbr)} vs {SIDE_EXISTING}={_human_bps(ex_vbr)})"
            )
            return keep, "video", reasons, confidence
    else:
        confidence = "low"

    # 2) AUDIO tie-breaker
    bn = _best_audio(new)
    be = _best_audio(existing)
    if not bn or not be:
        confidence = "low"
        return "TIE", "tie", ["video and audio could not be compared reliably"], confidence

    if _codec_rank(bn.codec) != _codec_rank(be.codec):
        keep = SIDE_NEW if _codec_rank(bn.codec) < _codec_rank(be.codec) else SIDE_EXISTING
        reasons.append(f"{keep}: better audio codec ({SIDE_NEW}={bn.codec} vs {SIDE_EXISTING}={be.codec})")
        return keep, "audio", reasons, confidence

    if bn.atmos != be.atmos:
        keep = SIDE_NEW if bn.atmos else SIDE_EXISTING
        reasons.append(f"{keep}: Atmos present ({SIDE_NEW}={bn.atmos} vs {SIDE_EXISTING}={be.atmos})")
        return keep, "audio", reasons, confidence

    new_ch = _audio_channels_count(bn.channels)
    ex_ch = _audio_channels_count(be.channels)
    if new_ch != ex_ch:
        keep = SIDE_NEW if new_ch > ex_ch else SIDE_EXISTING
        reasons.append(f"{keep}: more audio channels ({SIDE_NEW}={bn.channels} vs {SIDE_EXISTING}={be.channels})")
        confidence = "medium" if confidence == "high" else confidence
        return keep, "audio", reasons, confidence

    return "TIE", "tie", [f"{SIDE_NEW} and {SIDE_EXISTING} appear equivalent for video-first rules"], confidence


def _fmt_duration(dur_s: Optional[float]) -> str:
    if dur_s is None:
        return "?"
    m, s = divmod(int(dur_s + 0.5), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _truncate(s: str, width: int) -> str:
    s = s or ""
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width <= 1:
        return s[:width]
    return s[: width - 1] + "…"


def _clean_reason(reason: str) -> str:
    r = (reason or "").strip()
    for p in (f"{SIDE_NEW}:", f"{SIDE_EXISTING}:"):
        if r.startswith(p):
            r = r[len(p):].lstrip()
    return r


@dataclass(frozen=True)
class CompareResult:
    key: Tuple[str, str]
    a_ent: MovieEntry
    b_ent: MovieEntry
    a_sum: Optional[MediaSummary]
    b_sum: Optional[MediaSummary]
    notes: Tuple[str, ...]
    keep: str
    basis: str
    reasons: Tuple[str, ...]
    confidence: str


def _is_applicable(r: CompareResult) -> bool:
    """Eligible for --apply: high-confidence, imdb-keyed, non-tie, and both sides probed."""
    return (
        r.keep in (SIDE_NEW, SIDE_EXISTING)
        and r.confidence == "high"
        and r.key[0] == "imdb"
        and r.a_sum is not None
        and r.b_sum is not None
    )


def _planned_action(r: CompareResult) -> str:
    if not _is_applicable(r):
        return "skip"
    return "replace_existing" if r.keep == SIDE_NEW else "delete_new"


def _write_plan_file(
    path: str,
    path_new: str,
    path_existing: List[str],
    only_new_keys: List[Tuple[str, str]],
    a_index: Dict[Tuple[str, str], List[MovieEntry]],
    results: List[CompareResult],
    ambiguous_keys: List[Tuple[str, str]],
) -> None:
    actions = []
    for r in results:
        actions.append({
            "key_type": r.key[0],
            "key_value": r.key[1],
            "imdb": r.a_ent.imdb or r.b_ent.imdb,
            "title": r.a_ent.title or r.b_ent.title,
            "year": r.a_ent.year or r.b_ent.year,
            "keep": r.keep,
            "basis": r.basis,
            "confidence": r.confidence,
            "reasons": list(r.reasons),
            "notes": list(r.notes),
            "new_folder": r.a_ent.folder,
            "existing_folder": r.b_ent.folder,
            "applicable": _is_applicable(r),
            "action": _planned_action(r),
        })

    plan = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "path_new": path_new,
        "path_existing": path_existing,
        "only_in_new": [
            {"key_type": k[0], "key_value": k[1], "folder": e.folder}
            for k in only_new_keys
            for e in a_index.get(k, [])
        ],
        "ambiguous": [{"key_type": k[0], "key_value": k[1]} for k in ambiguous_keys],
        "actions": actions,
    }

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)


def _fix_perms_on(folder: str) -> None:
    """Shell out to fix_media_perms.py --apply on a single folder. We are already root when called."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_media_perms.py")
    subprocess.run([sys.executable, script, "--apply", folder], check=True)


def _apply_one(r: CompareResult) -> Tuple[bool, str]:
    """Execute a single applicable result. Returns (applied, message).

    Replacement always happens in place: the target root is derived from the
    matched EXISTING folder's own parent directory, not a fixed library path.
    This matters once EXISTING spans multiple roots (e.g. movies + movies2) —
    a match found in movies2 must stay in movies2, not get relocated to movies.
    """
    title_str = r.a_ent.title or r.b_ent.title or r.key[1]
    year_str = r.a_ent.year or r.b_ent.year or ""
    label = f"{title_str} ({year_str})".strip()

    if r.keep == SIDE_NEW:
        new_folder = os.path.normpath(r.a_ent.folder)
        existing_folder = os.path.normpath(r.b_ent.folder)
        library_root = os.path.dirname(existing_folder)
        target = os.path.join(library_root, os.path.basename(new_folder))

        # Safety: if the target name collides with some third folder (not the existing match), refuse.
        if (
            os.path.abspath(target) != os.path.abspath(existing_folder)
            and os.path.exists(target)
        ):
            return False, f"target exists and is not the matched existing folder: {target}"

        _fix_perms_on(new_folder)
        if os.path.isdir(existing_folder):
            shutil.rmtree(existing_folder)
        shutil.move(new_folder, target)
        return True, f"KEEP NEW  :: {label} -> {target}"

    if r.keep == SIDE_EXISTING:
        new_folder = r.a_ent.folder
        shutil.rmtree(new_folder)
        return True, f"KEEP EXISTING :: {label} (deleted {new_folder})"

    return False, "unsupported keep value"


def _apply_results(results: List[CompareResult]) -> Tuple[int, int, int]:
    applied = 0
    skipped = 0
    errors = 0
    for r in results:
        if not _is_applicable(r):
            skipped += 1
            continue
        try:
            ok, msg = _apply_one(r)
        except Exception as e:
            errors += 1
            print(f"[APPLY-ERR] {r.key[0]}:{r.key[1]}: {e}")
            continue
        if ok:
            applied += 1
            print(f"[APPLY] {msg}")
        else:
            skipped += 1
            print(f"[APPLY-SKIP] {r.key[0]}:{r.key[1]}: {msg}")
    return applied, skipped, errors


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare movie copies in two libraries; optionally apply HIGH-confidence recommendations.",
        epilog=(
            "SSH tip: add 'ControlMaster auto' + 'ControlPersist 60s' to ~/.ssh/config "
            "to share connections and avoid per-probe handshake overhead."
        ),
    )
    ap.add_argument("--path-a", default=DEFAULT_NEW_PATH, help=f"New copies path (default: {DEFAULT_NEW_PATH})")
    ap.add_argument(
        "--path-b",
        default=DEFAULT_EXISTING_PATH,
        help=(
            f"Existing library path(s) (default: {DEFAULT_EXISTING_PATH}). "
            "Comma-separate multiple roots (e.g. /storage/media/movies,/storage/media/movies2) "
            "to check newly downloaded movies against all of them at once."
        ),
    )
    ap.add_argument("--ssh-a", metavar="USER@HOST", default=None,
                    help="SSH host for path-a (e.g. david@192.168.33.40). Omit for local.")
    ap.add_argument("--ssh-b", metavar="USER@HOST", default=None,
                    help="SSH host for path-b (e.g. david@192.168.36.40). Omit for local.")
    ap.add_argument("--threads", type=int, default=8, help="Concurrent probes for matched movies (default: 8)")
    ap.add_argument(
        "--details",
        dest="details",
        action="store_true",
        help="Show full per-movie technical details (Section 3).",
    )
    ap.add_argument(
        "--strict-imdb",
        action="store_true",
        help="Only match movies by IMDb id (skip title/year matches).",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Execute HIGH-confidence, imdb-keyed recommendations (destructive): "
            "fix perms on NEW + replace EXISTING, or delete NEW. "
            "Requires root; auto re-execs under sudo."
        ),
    )
    args = ap.parse_args()

    # --apply executes local shutil.rmtree/move on the indexed paths; running it
    # against paths that were enumerated over SSH would delete/move same-named
    # LOCAL folders instead of the remote ones. Refuse the combination outright.
    if args.apply and (args.ssh_a or args.ssh_b):
        print("ERROR: --apply only supports local paths (it runs local filesystem "
              "operations). Re-run without --ssh-a/--ssh-b on the host that owns "
              "the libraries.", file=sys.stderr)
        return 2

    # --apply is destructive and needs root (for chown via fix_media_perms and for moving into /storage/media/movies).
    if args.apply and os.geteuid() != 0:
        if shutil.which("sudo") is None:
            print("ERROR: --apply requires root and 'sudo' was not found.", file=sys.stderr)
            return 2
        os.execvp("sudo", ["sudo", sys.executable] + sys.argv)

    shell_a = Shell(host=args.ssh_a)
    shell_b = Shell(host=args.ssh_b)

    # Check required binaries on each side (mkvextract needed for IMDb tag reads)
    missing: List[str] = []
    for label, sh in ((SIDE_NEW, shell_a), (SIDE_EXISTING, shell_b)):
        for b in ("ffprobe", "mkvmerge", "mkvextract"):
            if not sh.which_ok(b):
                missing.append(f"{b} (on {label} {'SSH:' + sh.host if sh.host else 'local'})")
    if missing:
        print(f"ERROR: missing required binaries: {', '.join(missing)}", file=sys.stderr)
        return 2

    path_b_roots = [p.strip() for p in args.path_b.split(",") if p.strip()]

    # An empty root is never a valid comparison input — it means a typo'd path,
    # an unmounted dataset, or a failed SSH listing (list_subdirs swallows remote
    # errors into an empty result). Proceeding would mislabel every EXISTING
    # movie as net-new, and that output feeds radarr_upgrade_push.py, which
    # would re-download the whole library. Abort instead.
    for root, sh, side in [(args.path_a, shell_a, SIDE_NEW)] + [
            (r, shell_b, SIDE_EXISTING) for r in path_b_roots]:
        if not sh.list_subdirs(root):
            print(f"ERROR: {side} root has no movie folders (missing path, empty "
                  f"dataset, or failed SSH listing): {root}", file=sys.stderr)
            return 2

    print("=== Compare Movie Libraries ===")
    print(f"{SIDE_NEW} path:      {args.path_a}  {'[SSH: ' + args.ssh_a + ']' if args.ssh_a else '[local]'}")
    print(f"{SIDE_EXISTING} path: {', '.join(path_b_roots)}  {'[SSH: ' + args.ssh_b + ']' if args.ssh_b else '[local]'}")
    print()

    a_index, a_problems = index_library(args.path_a, SIDE_NEW, shell_a, threads=args.threads)
    b_index, b_problems = index_libraries(path_b_roots, SIDE_EXISTING, shell_b, threads=args.threads)
    for p in a_problems + b_problems:
        print(p)

    if args.strict_imdb:
        a_index = {k: v for k, v in a_index.items() if k[0] == "imdb"}
        b_index = {k: v for k, v in b_index.items() if k[0] == "imdb"}

    a_keys = set(a_index.keys())
    b_keys = set(b_index.keys())

    only_a = sorted(a_keys - b_keys)
    both = sorted(a_keys & b_keys)

    ambiguous: List[Tuple[str, str]] = []
    for k in both:
        if len(a_index.get(k, [])) != 1 or len(b_index.get(k, [])) != 1:
            ambiguous.append(k)
    both_unique = [k for k in both if k not in set(ambiguous)]

    print(f"=== 1) Movies in {SIDE_NEW} that are NOT in {SIDE_EXISTING} ===")
    print(f"Count: {len(only_a)}")
    if not only_a:
        print("(none)")
    else:
        for k in only_a:
            for e in a_index.get(k, []):
                warn = f" [{' ,'.join(e.warnings)}]" if e.warnings else ""
                print(f"- {k[0]}:{k[1]}  ::  {_fmt_identity(e)}  ::  {os.path.basename(e.folder)}{warn}")
    print()

    print(f"=== 2) Movies present in BOTH ({SIDE_NEW} vs {SIDE_EXISTING}) (recommendation summary) ===")
    print(f"Matched (unique): {len(both_unique)}")
    if ambiguous:
        print(f"Ambiguous keys (need manual review): {len(ambiguous)}")
    print()

    def probe_pair(k: Tuple[str, str]) -> Tuple[Tuple[str, str], Optional[MediaSummary], Optional[MediaSummary], List[str]]:
        notes: List[str] = []
        a_ent = a_index[k][0]
        b_ent = b_index[k][0]
        if not a_ent.mkv_path:
            notes.append("A:no_mkv")
            return k, None, None, notes
        if not b_ent.mkv_path:
            notes.append("B:no_mkv")
            return k, None, None, notes
        try:
            a_sum = summarize_media(a_ent.mkv_path, shell_a)
        except Exception as e:
            notes.append(f"A:probe_failed({e})")
            a_sum = None
        try:
            b_sum = summarize_media(b_ent.mkv_path, shell_b)
        except Exception as e:
            notes.append(f"B:probe_failed({e})")
            b_sum = None
        return k, a_sum, b_sum, notes


    def label_for(k: Tuple[str, str], a_ent: MovieEntry, b_ent: MovieEntry) -> str:
        ident = _fmt_identity(a_ent) or _fmt_identity(b_ent)
        imdb = a_ent.imdb or b_ent.imdb
        if imdb:
            if ident.startswith("tt"):
                if (a_ent.title and a_ent.year) or (b_ent.title and b_ent.year):
                    ident = _fmt_identity(a_ent) if (a_ent.title and a_ent.year) else _fmt_identity(b_ent)
            return f"{k[0]}:{k[1]}  ::  {imdb}  ::  {ident}"
        return f"{k[0]}:{k[1]}  ::  {ident}"

    results: List[CompareResult] = []
    if not both_unique:
        print("(none)")
    else:
        with cf.ThreadPoolExecutor(max_workers=max(1, int(args.threads))) as ex:
            futs = {ex.submit(probe_pair, k): k for k in both_unique}
            for fut in cf.as_completed(futs):
                k, a_sum, b_sum, notes = fut.result()
                a_ent = a_index[k][0]
                b_ent = b_index[k][0]

                keep = "TIE"
                basis = "tie"
                conf = "low"
                reasons: List[str] = []
                if a_sum and b_sum:
                    keep, basis, reasons, conf = recommend_keep(a_sum, b_sum)
                else:
                    reasons = [f"probe failed for {SIDE_NEW} or {SIDE_EXISTING}"]

                results.append(
                    CompareResult(
                        key=k,
                        a_ent=a_ent,
                        b_ent=b_ent,
                        a_sum=a_sum,
                        b_sum=b_sum,
                        notes=tuple(notes),
                        keep=keep,
                        basis=basis,
                        reasons=tuple(reasons),
                        confidence=conf,
                    )
                )

    results.sort(key=lambda r: (r.key[0], r.key[1]))
    keep_new = sum(1 for r in results if r.keep == SIDE_NEW)
    keep_existing = sum(1 for r in results if r.keep == SIDE_EXISTING)
    ties = sum(1 for r in results if r.keep == "TIE")
    print(f"Summary: KEEP {SIDE_NEW}={keep_new}  KEEP {SIDE_EXISTING}={keep_existing}  TIE={ties}")
    print()

    action_order = {SIDE_NEW: 0, SIDE_EXISTING: 1, "TIE": 2}

    def sort_title(r: CompareResult) -> str:
        t = r.a_ent.title or r.b_ent.title or ""
        y = r.a_ent.year or r.b_ent.year or ""
        return f"{t} {y}".strip().lower()

    results_sorted = sorted(results, key=lambda r: (action_order.get(r.keep, 9), sort_title(r)))

    w_action = 12
    w_basis = 6
    w_conf = 6
    w_title = 38
    w_imdb = 10
    w_root = 12
    header = (
        f"{'ACTION':<{w_action}}  "
        f"{'BASIS':<{w_basis}}  "
        f"{'CONF':<{w_conf}}  "
        f"{'TITLE':<{w_title}}  "
        f"{'IMDB':<{w_imdb}}  "
        f"{'EXIST ROOT':<{w_root}}  "
        f"WHY"
    )
    print(header)
    print("-" * len(header))

    for r in results_sorted:
        title = _fmt_identity(r.a_ent) if (r.a_ent.title and r.a_ent.year) else _fmt_identity(r.b_ent)
        imdb = r.a_ent.imdb or r.b_ent.imdb or ""
        # Which EXISTING root this match actually lives under (e.g. movies vs movies2) —
        # matters once path-b spans more than one root.
        existing_root = os.path.basename(os.path.dirname(os.path.normpath(r.b_ent.folder)))

        action = "TIE" if r.keep == "TIE" else f"KEEP {r.keep}"
        basis = "-" if r.keep == "TIE" else r.basis
        conf = r.confidence
        why = _clean_reason(r.reasons[0]) if r.reasons else ""
        if r.keep in {SIDE_NEW, SIDE_EXISTING} and len(r.reasons) > 1:
            why = why + " (+more)"
        if r.notes:
            why = (why + f" [notes:{len(r.notes)}]").strip()

        print(
            f"{action:<{w_action}}  "
            f"{basis:<{w_basis}}  "
            f"{conf:<{w_conf}}  "
            f"{_truncate(title, w_title):<{w_title}}  "
            f"{_truncate(imdb, w_imdb):<{w_imdb}}  "
            f"{_truncate(existing_root, w_root):<{w_root}}  "
            f"{why}"
        )
    print()

    if args.details:
        print(f"=== 3) Movies present in BOTH ({SIDE_NEW} vs {SIDE_EXISTING}) (full details) ===")
        print(f"Count: {len(results)}")
        print()
        if not results:
            print("(none)")
        else:
            for r in results:
                print(f"--- {label_for(r.key, r.a_ent, r.b_ent)}")
                print(f" {SIDE_NEW} folder: {os.path.normpath(r.a_ent.folder)}")
                print(f" {SIDE_EXISTING} folder: {os.path.normpath(r.b_ent.folder)}")

                if r.a_sum:
                    print(f" {SIDE_NEW} file: {os.path.basename(r.a_sum.mkv_path)}")
                    print(f"   size={_human_bytes(r.a_sum.size_bytes)}  dur={_fmt_duration(r.a_sum.duration_s)}")
                    print(f"   {_fmt_video(r.a_sum)}")
                    print(f"   {_fmt_hdr(r.a_sum)}")
                    print(f"   {_fmt_audio_tracks(r.a_sum)}")
                else:
                    print(f" {SIDE_NEW} file: (probe failed)")

                if r.b_sum:
                    print(f" {SIDE_EXISTING} file: {os.path.basename(r.b_sum.mkv_path)}")
                    print(f"   size={_human_bytes(r.b_sum.size_bytes)}  dur={_fmt_duration(r.b_sum.duration_s)}")
                    print(f"   {_fmt_video(r.b_sum)}")
                    print(f"   {_fmt_hdr(r.b_sum)}")
                    print(f"   {_fmt_audio_tracks(r.b_sum)}")
                else:
                    print(f" {SIDE_EXISTING} file: (probe failed)")

                if r.keep in {SIDE_NEW, SIDE_EXISTING}:
                    print(f" RECOMMEND: KEEP {r.keep} (basis={r.basis}, confidence={r.confidence})")
                else:
                    print(f" RECOMMEND: TIE (confidence={r.confidence})")
                for reason in r.reasons:
                    print(f"  - {reason}")
                if r.notes:
                    print(" Notes: " + " ; ".join(r.notes))
                print()

    if ambiguous:
        print("=== Ambiguous matches (skipped from detailed comparison) ===")
        for k in ambiguous:
            print(f"- {k[0]}:{k[1]}")
            print("  A candidates:")
            for e in a_index.get(k, []):
                print(f"   - {os.path.basename(e.folder)} :: {_fmt_identity(e)}")
            print("  B candidates:")
            for e in b_index.get(k, []):
                print(f"   - {os.path.basename(e.folder)} :: {_fmt_identity(e)}")
        print()

    os.makedirs(LOG_DIR, exist_ok=True)
    plan_path = os.path.join(LOG_DIR, f"compare_plan_{time.strftime('%Y%m%d_%H%M%S')}.json")
    _write_plan_file(
        plan_path,
        args.path_a,
        path_b_roots,
        only_a,
        a_index,
        results,
        ambiguous,
    )
    print(f"Plan written to: {plan_path}")
    applicable_count = sum(1 for r in results if _is_applicable(r))
    print(f"Applicable (HIGH-confidence, imdb-keyed): {applicable_count} / {len(results)}")

    if args.apply:
        print()
        print("=== APPLY (destructive) ===")
        applied, skipped, errors = _apply_results(results)
        print()
        print(f"Apply summary: applied={applied}  skipped={skipped}  errors={errors}")
        if errors:
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
