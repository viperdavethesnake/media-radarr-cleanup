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


TV_ROOT = Path(os.getenv("RC_MEDIA_BASE", "/storage/media")) / "tvshows"
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts"}

# Accept either naming convention; your tv pipeline writes show.nfo, Jellyfin often uses tvshow.nfo
SHOW_NFO_NAMES = {"show.nfo", "tvshow.nfo"}
REQUIRED_SHOW_FILES = SHOW_NFO_NAMES | {"poster.jpg"}
INTERMEDIATE_SHOW_FILES = {"metadata.json", "tags.xml"}


@dataclass
class Finding:
    kind: str  # orphan_show_folder | missing_required_show_files | has_intermediate_show_files
    show_directory: str
    has_video_in_subtree: bool
    present_files: List[str]
    missing_required: List[str]


def _is_video_name(filename: str) -> bool:
    return Path(filename).suffix.lower() in VIDEO_EXTS


def _list_dir(dirpath: Path) -> Optional[List[str]]:
    try:
        return [e.name for e in os.scandir(dirpath) if not e.is_symlink()]
    except (FileNotFoundError, PermissionError):
        return None


def _has_video_in_subtree(show_dir: Path) -> bool:
    for dirpath, _, filenames in os.walk(show_dir, topdown=True, followlinks=False):
        for fn in filenames:
            if _is_video_name(fn):
                return True
    return False


def scan(tv_root: Path) -> List[Finding]:
    findings: List[Finding] = []
    if not tv_root.exists() or not tv_root.is_dir():
        return findings

    # Treat immediate children as "show folders"
    try:
        show_dirs = [p for p in tv_root.iterdir() if p.is_dir() and not p.is_symlink()]
    except Exception:
        return findings

    for show_dir in sorted(show_dirs):
        names = _list_dir(show_dir) or []
        lower = {n.lower() for n in names}

        has_video = _has_video_in_subtree(show_dir)

        # Determine if we have a show-level NFO (either name)
        has_show_nfo = any(n in lower for n in SHOW_NFO_NAMES)
        missing_required: List[str] = []
        if not has_show_nfo:
            missing_required.append("show.nfo|tvshow.nfo")
        if "poster.jpg" not in lower:
            missing_required.append("poster.jpg")

        present = sorted([n for n in names if n.lower() in REQUIRED_SHOW_FILES or n.lower() in INTERMEDIATE_SHOW_FILES])

        # Orphan: metadata present but no video anywhere in show subtree
        if (present or lower.intersection(SHOW_NFO_NAMES) or "poster.jpg" in lower) and not has_video:
            findings.append(
                Finding(
                    kind="orphan_show_folder",
                    show_directory=str(show_dir),
                    has_video_in_subtree=False,
                    present_files=present,
                    missing_required=[],
                )
            )

        # Missing required: has video but missing show-level files
        if has_video and missing_required:
            findings.append(
                Finding(
                    kind="missing_required_show_files",
                    show_directory=str(show_dir),
                    has_video_in_subtree=True,
                    present_files=present,
                    missing_required=missing_required,
                )
            )

        # Intermediate leftovers: show-level intermediate files present
        inter = sorted([n for n in names if n.lower() in INTERMEDIATE_SHOW_FILES])
        if inter:
            findings.append(
                Finding(
                    kind="has_intermediate_show_files",
                    show_directory=str(show_dir),
                    has_video_in_subtree=has_video,
                    present_files=inter,
                    missing_required=[],
                )
            )

    return findings


def write_json(findings: List[Finding], out_path: Path) -> None:
    out_path.write_text(json.dumps([asdict(f) for f in findings], indent=2), encoding="utf-8")


def write_csv(findings: List[Finding], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh, fieldnames=["kind", "show_directory", "has_video_in_subtree", "present_files", "missing_required"]
        )
        w.writeheader()
        for f in findings:
            w.writerow(
                {
                    "kind": f.kind,
                    "show_directory": f.show_directory,
                    "has_video_in_subtree": str(f.has_video_in_subtree),
                    "present_files": " | ".join(f.present_files),
                    "missing_required": " | ".join(f.missing_required),
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "TV show folder report (report-only).\n\n"
            f"For each top-level show folder under {TV_ROOT}:\n"
            "- If any episode video exists anywhere in the show subtree, the show folder should contain:\n"
            "    - poster.jpg\n"
            "    - show.nfo OR tvshow.nfo\n"
            "- If show.nfo/tvshow.nfo/poster.jpg exist but there are no episodes anywhere in the subtree => orphan show folder.\n"
            "- If metadata.json/tags.xml exist at show root => intermediate leftovers (flagged).\n"
        )
    )
    parser.add_argument(
        "--root",
        default=str(TV_ROOT),
        help=f"TV root directory (default: {TV_ROOT})",
    )
    parser.add_argument("--json", dest="json_path", help="Write findings to JSON file.")
    parser.add_argument("--csv", dest="csv_path", help="Write findings to CSV file.")
    args = parser.parse_args()

    tv_root = Path(args.root).expanduser().resolve()
    print("Mode: REPORT-ONLY (no deletion)")
    print(f"TV root: {tv_root}")

    findings = scan(tv_root)
    orphan = [f for f in findings if f.kind == "orphan_show_folder"]
    missing = [f for f in findings if f.kind == "missing_required_show_files"]
    inter = [f for f in findings if f.kind == "has_intermediate_show_files"]

    print("\nSummary:")
    print(f"  orphan show folders         : {len(orphan)}")
    print(f"  missing required show files : {len(missing)}")
    print(f"  intermediate leftovers found: {len(inter)}")

    if args.json_path:
        write_json(findings, Path(args.json_path).expanduser())
        print(f"  wrote JSON: {args.json_path}")
    if args.csv_path:
        write_csv(findings, Path(args.csv_path).expanduser())
        print(f"  wrote CSV : {args.csv_path}")

    if orphan:
        print("\nOrphan show folders (show-level metadata exists, but no episodes anywhere in subtree):")
        for f in orphan[:200]:
            print(f"  - {f.show_directory}  present: {', '.join(f.present_files) if f.present_files else '(none?)'}")
        if len(orphan) > 200:
            print(f"  ... and {len(orphan) - 200} more")

    if missing:
        print("\nShows missing required show-level files (episodes exist in subtree):")
        for f in missing[:200]:
            print(f"  - {f.show_directory}  missing: {', '.join(f.missing_required)}")
        if len(missing) > 200:
            print(f"  ... and {len(missing) - 200} more")

    if inter:
        print("\nShows with intermediate leftovers at show root (metadata.json/tags.xml):")
        for f in inter[:200]:
            print(f"  - {f.show_directory}  leftovers: {', '.join(f.present_files)}  has_video={f.has_video_in_subtree}")
        if len(inter) > 200:
            print(f"  ... and {len(inter) - 200} more")

    print("\nNote: report-only; no changes are made.")


if __name__ == "__main__":
    main()

