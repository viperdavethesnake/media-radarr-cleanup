#!/usr/bin/env python3

import argparse
import csv
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()


BASE = Path(os.getenv("RC_MEDIA_BASE", "/storage/media"))
DEFAULT_ROOTS = [
    BASE / "movies",
    BASE / "documentaries",
]

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts"}
REQUIRED_MOVIE_FILES = {"movie.nfo", "poster.jpg"}
INTERMEDIATE_FILES = {"metadata.json", "tags.xml"}


@dataclass
class Finding:
    kind: str  # orphan_movie_folder | missing_required_files | has_intermediate_files
    directory: str
    video_files: List[str]
    metadata_files: List[str]
    missing_required: List[str]


def _is_video_name(filename: str) -> bool:
    return Path(filename).suffix.lower() in VIDEO_EXTS


def expand_roots(paths: List[str]) -> List[Path]:
    if not paths:
        return DEFAULT_ROOTS[:]
    return [Path(p).expanduser().resolve() for p in paths]


def _list_dir_lower(dirpath: Path) -> Optional[List[str]]:
    try:
        return [e.name for e in os.scandir(dirpath) if not e.is_symlink()]
    except FileNotFoundError:
        return None
    except PermissionError:
        return None


def scan(roots: List[Path]) -> List[Finding]:
    """
    Movie/doc contract (matches batch_cleaner -> mkv_remux_cleanroom):
      - A movie folder should contain at least one video file (usually one .mkv)
      - If it contains video, it should also contain: movie.nfo + poster.jpg
      - Intermediate artifacts (should not be in final library): metadata.json, tags.xml
      - If it contains movie.nfo and/or poster.jpg but no video in that same folder: orphan movie folder
    """
    findings: List[Finding] = []

    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(root, topdown=True, followlinks=False):
            d = Path(dirpath)

            # Skip root itself; we care about per-movie folders.
            if d == root:
                continue

            names = _list_dir_lower(d)
            if not names:
                continue

            lower = {n.lower() for n in names}
            videos = [n for n in names if _is_video_name(n)]

            has_required_markers = bool(lower.intersection(REQUIRED_MOVIE_FILES))

            # Orphan: required markers present but no video in the same folder.
            if has_required_markers and not videos:
                present_meta = sorted([n for n in names if n.lower() in REQUIRED_MOVIE_FILES or n.lower() in INTERMEDIATE_FILES])
                findings.append(
                    Finding(
                        kind="orphan_movie_folder",
                        directory=str(d),
                        video_files=[],
                        metadata_files=present_meta,
                        missing_required=[],
                    )
                )

            # Missing required: video in folder, but missing required files.
            if videos:
                missing = sorted([req for req in REQUIRED_MOVIE_FILES if req not in lower])
                if missing:
                    findings.append(
                        Finding(
                            kind="missing_required_files",
                            directory=str(d),
                            video_files=sorted(videos),
                            metadata_files=sorted([n for n in names if n.lower() in REQUIRED_MOVIE_FILES]),
                            missing_required=missing,
                        )
                    )

                # Intermediate leftovers: flag if present next to video.
                inter = sorted([n for n in names if n.lower() in INTERMEDIATE_FILES])
                if inter:
                    findings.append(
                        Finding(
                            kind="has_intermediate_files",
                            directory=str(d),
                            video_files=sorted(videos),
                            metadata_files=inter,
                            missing_required=[],
                        )
                    )

    return findings


def write_json(findings: List[Finding], out_path: Path) -> None:
    out_path.write_text(json.dumps([asdict(f) for f in findings], indent=2), encoding="utf-8")


def write_csv(findings: List[Finding], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh, fieldnames=["kind", "directory", "video_files", "metadata_files", "missing_required"]
        )
        w.writeheader()
        for f in findings:
            w.writerow(
                {
                    "kind": f.kind,
                    "directory": f.directory,
                    "video_files": " | ".join(f.video_files),
                    "metadata_files": " | ".join(f.metadata_files),
                    "missing_required": " | ".join(f.missing_required),
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Movie/Documentary folder report (report-only).\n\n"
            "Contract (matches batch_cleaner.py -> mkv_remux_cleanroom.py):\n"
            "- Folder contains video => must also contain: movie.nfo + poster.jpg\n"
            "- Folder contains movie.nfo/poster.jpg but no video => orphan\n"
            "- Folder contains metadata.json/tags.xml next to video => intermediate leftovers\n\n"
            f"Defaults to scanning {BASE}/{{movies,documentaries}}."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=f"Optional root paths to scan. If omitted, uses {BASE}/{{movies,documentaries}}.",
    )
    parser.add_argument("--json", dest="json_path", help="Write findings to JSON file.")
    parser.add_argument("--csv", dest="csv_path", help="Write findings to CSV file.")
    args = parser.parse_args()

    roots = expand_roots(args.paths)

    print("Mode: REPORT-ONLY (no deletion)")
    print("Roots:")
    for r in roots:
        print(f"  - {r}")

    print("\nScanning...")
    findings = scan(roots)

    orphan = [f for f in findings if f.kind == "orphan_movie_folder"]
    missing = [f for f in findings if f.kind == "missing_required_files"]
    intermediate = [f for f in findings if f.kind == "has_intermediate_files"]

    print("\nSummary:")
    print(f"  orphan movie folders        : {len(orphan)}")
    print(f"  missing required files      : {len(missing)}")
    print(f"  intermediate leftovers found: {len(intermediate)}")

    if args.json_path:
        write_json(findings, Path(args.json_path).expanduser())
        print(f"  wrote JSON: {args.json_path}")
    if args.csv_path:
        write_csv(findings, Path(args.csv_path).expanduser())
        print(f"  wrote CSV : {args.csv_path}")

    if orphan:
        print("\nOrphan movie folders (movie.nfo/poster.jpg present, but no video file in same folder):")
        for f in orphan[:200]:
            print(f"  - {f.directory}  files: {', '.join(f.metadata_files) if f.metadata_files else '(none?)'}")
        if len(orphan) > 200:
            print(f"  ... and {len(orphan) - 200} more")

    if missing:
        print("\nFolders missing required files (video present, missing movie.nfo and/or poster.jpg):")
        for f in missing[:200]:
            print(f"  - {f.directory}  missing: {', '.join(f.missing_required)}  video: {', '.join(f.video_files[:3])}")
        if len(missing) > 200:
            print(f"  ... and {len(missing) - 200} more")

    if intermediate:
        print("\nFolders with intermediate leftovers (metadata.json/tags.xml next to video):")
        for f in intermediate[:200]:
            print(f"  - {f.directory}  leftovers: {', '.join(f.metadata_files)}")
        if len(intermediate) > 200:
            print(f"  ... and {len(intermediate) - 200} more")
    print("\nNote: report-only; no changes are made.")


if __name__ == "__main__":
    main()

