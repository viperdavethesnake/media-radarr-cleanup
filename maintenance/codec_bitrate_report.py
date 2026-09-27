#!/usr/bin/env python3

import argparse
import csv
import json
import os
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from shutil import which
from typing import List, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()


BASE = Path(os.getenv("RC_MEDIA_BASE", "/storage/media"))
DEFAULT_ROOTS = [
    BASE / "movies",
    BASE / "documentaries",
    BASE / "tvshows",
]

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts"}


@dataclass
class MediaRow:
    path: str
    size_bytes: int
    duration_s: Optional[float]
    container: Optional[str]
    overall_bitrate_bps: Optional[int]
    video_codec: Optional[str]
    width: Optional[int]
    height: Optional[int]
    video_bitrate_bps: Optional[int]
    audio_codec: Optional[str]
    audio_channels: Optional[int]


def is_video_file(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXTS


def expand_roots(paths: List[str]) -> List[Path]:
    if not paths:
        return DEFAULT_ROOTS[:]
    return [Path(p).expanduser().resolve() for p in paths]


def _run(cmd: List[str]) -> Tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def probe_with_ffprobe(path: Path) -> Optional[MediaRow]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    rc, out, err = _run(cmd)
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []

    duration_s = None
    try:
        if fmt.get("duration") is not None:
            duration_s = float(fmt["duration"])
    except Exception:
        duration_s = None

    container = fmt.get("format_name")
    overall_bitrate_bps = None
    try:
        if fmt.get("bit_rate") is not None:
            overall_bitrate_bps = int(float(fmt["bit_rate"]))
    except Exception:
        overall_bitrate_bps = None

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    video_codec = video.get("codec_name") if video else None
    width = int(video["width"]) if video and video.get("width") is not None else None
    height = int(video["height"]) if video and video.get("height") is not None else None

    video_bitrate_bps = None
    try:
        if video and video.get("bit_rate") is not None:
            video_bitrate_bps = int(float(video["bit_rate"]))
    except Exception:
        video_bitrate_bps = None

    audio_codec = audio.get("codec_name") if audio else None
    audio_channels = None
    try:
        if audio and audio.get("channels") is not None:
            audio_channels = int(audio["channels"])
    except Exception:
        audio_channels = None

    try:
        size_bytes = path.stat().st_size
    except Exception:
        size_bytes = 0

    # If no overall bitrate, estimate from size/duration
    if overall_bitrate_bps is None and duration_s and duration_s > 0 and size_bytes > 0:
        overall_bitrate_bps = int((size_bytes * 8) / duration_s)

    return MediaRow(
        path=str(path),
        size_bytes=size_bytes,
        duration_s=duration_s,
        container=container,
        overall_bitrate_bps=overall_bitrate_bps,
        video_codec=video_codec,
        width=width,
        height=height,
        video_bitrate_bps=video_bitrate_bps,
        audio_codec=audio_codec,
        audio_channels=audio_channels,
    )


def probe_with_mediainfo(path: Path) -> Optional[MediaRow]:
    cmd = ["mediainfo", "--Output=JSON", str(path)]
    rc, out, err = _run(cmd)
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None

    media = (data.get("media") or {})
    tracks = media.get("track") or []

    general = next((t for t in tracks if t.get("@type") == "General"), None)
    video = next((t for t in tracks if t.get("@type") == "Video"), None)
    audio = next((t for t in tracks if t.get("@type") == "Audio"), None)

    def to_int(x) -> Optional[int]:
        try:
            if x is None:
                return None
            return int(float(str(x).strip()))
        except Exception:
            return None

    def to_float(x) -> Optional[float]:
        try:
            if x is None:
                return None
            return float(str(x).strip())
        except Exception:
            return None

    try:
        size_bytes = path.stat().st_size
    except Exception:
        size_bytes = 0

    duration_ms = to_float((general or {}).get("Duration"))
    duration_s = (duration_ms / 1000.0) if duration_ms else None

    container = (general or {}).get("Format")
    overall_bitrate_bps = to_int((general or {}).get("OverallBitRate"))

    video_codec = (video or {}).get("Format") or (video or {}).get("CodecID")
    width = to_int((video or {}).get("Width"))
    height = to_int((video or {}).get("Height"))
    video_bitrate_bps = to_int((video or {}).get("BitRate"))

    audio_codec = (audio or {}).get("Format") or (audio or {}).get("CodecID")
    audio_channels = to_int((audio or {}).get("Channels"))

    if overall_bitrate_bps is None and duration_s and duration_s > 0 and size_bytes > 0:
        overall_bitrate_bps = int((size_bytes * 8) / duration_s)

    return MediaRow(
        path=str(path),
        size_bytes=size_bytes,
        duration_s=duration_s,
        container=container,
        overall_bitrate_bps=overall_bitrate_bps,
        video_codec=video_codec,
        width=width,
        height=height,
        video_bitrate_bps=video_bitrate_bps,
        audio_codec=audio_codec,
        audio_channels=audio_channels,
    )


def probe(path: Path, prefer: str) -> Tuple[Optional[MediaRow], Optional[str]]:
    if prefer == "ffprobe":
        if which("ffprobe"):
            r = probe_with_ffprobe(path)
            return r, None if r else "ffprobe failed"
        if which("mediainfo"):
            r = probe_with_mediainfo(path)
            return r, None if r else "mediainfo failed"
        return None, "neither ffprobe nor mediainfo found"

    if prefer == "mediainfo":
        if which("mediainfo"):
            r = probe_with_mediainfo(path)
            return r, None if r else "mediainfo failed"
        if which("ffprobe"):
            r = probe_with_ffprobe(path)
            return r, None if r else "ffprobe failed"
        return None, "neither mediainfo nor ffprobe found"

    return None, f"unknown probe preference: {prefer}"


def scan_files(roots: List[Path]) -> List[Path]:
    files: List[Path] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for dirpath, _, filenames in os.walk(root, topdown=True, followlinks=False):
            d = Path(dirpath)
            for fn in filenames:
                p = d / fn
                if p.is_symlink():
                    continue
                if is_video_file(p):
                    files.append(p)
    return files


def write_json(rows: List[MediaRow], out_path: Path) -> None:
    out_path.write_text(json.dumps([asdict(r) for r in rows], indent=2), encoding="utf-8")


def write_csv(rows: List[MediaRow], out_path: Path) -> None:
    fields = list(MediaRow.__dataclass_fields__.keys())
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a codec/bitrate report for media files.\n\n"
            f"Defaults to scanning {BASE}/{{movies,documentaries,tvshows}}."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=f"Optional root paths to scan. If omitted, uses {BASE}/{{movies,documentaries,tvshows}}.",
    )
    parser.add_argument(
        "--prefer",
        choices=["ffprobe", "mediainfo"],
        default="ffprobe",
        help="Which tool to prefer when probing media (default: ffprobe).",
    )
    parser.add_argument("--json", dest="json_path", help="Write report to JSON file.")
    parser.add_argument("--csv", dest="csv_path", help="Write report to CSV file.")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of files processed (0 = no limit). Useful for quick tests.",
    )
    args = parser.parse_args()

    roots = expand_roots(args.paths)
    print("Roots:")
    for r in roots:
        print(f"  - {r}")
    print(f"Probe preference: {args.prefer}")

    files = scan_files(roots)
    files.sort()
    if args.limit and args.limit > 0:
        files = files[: args.limit]
    print(f"Media files found: {len(files)}")

    if not which("ffprobe") and not which("mediainfo"):
        raise SystemExit("Neither 'ffprobe' nor 'mediainfo' is installed.")

    rows: List[MediaRow] = []
    errors = 0
    for p in files:
        row, err = probe(p, prefer=args.prefer)
        if row is None:
            errors += 1
            if err:
                print(f"[ERR] {p}: {err}")
            continue
        rows.append(row)

    print(f"Probed successfully: {len(rows)}")
    print(f"Probe errors       : {errors}")

    if args.json_path:
        write_json(rows, Path(args.json_path).expanduser())
        print(f"Wrote JSON: {args.json_path}")
    if args.csv_path:
        write_csv(rows, Path(args.csv_path).expanduser())
        print(f"Wrote CSV : {args.csv_path}")

    # Small console summary (top 20 by size)
    if rows:
        rows_sorted = sorted(rows, key=lambda r: r.size_bytes, reverse=True)
        print("\nTop 20 by size:")
        for r in rows_sorted[:20]:
            dim = f"{r.width}x{r.height}" if r.width and r.height else "?x?"
            br = r.overall_bitrate_bps if r.overall_bitrate_bps is not None else 0
            print(
                f"  - {r.size_bytes/1e9:.2f} GB | {dim:9} | "
                f"v={r.video_codec or '?':8} a={r.audio_codec or '?':8} | "
                f"{br/1e6:.2f} Mbps | {r.path}"
            )

if __name__ == "__main__":
    main()

