#!/usr/bin/env python3
"""
Audit EXISTING movie/documentary libraries for workflow-consistency issues.

This is a *read-only* audit tool intended for your "older/current" libraries.

It focuses on folders that contain video content and reports issues like:
- Folder contains video but no MKV (mp4/avi/etc only)
- Folder missing required companion files (movie.nfo, poster.jpg)
- Folder contains multiple video files / multiple MKVs (ambiguous)
- NFO exists but is invalid or missing critical fields (optional)
- MKV has unexpected attachments/tags/multiple video tracks (optional, deep scan)

Defaults (override via CLI):
- Movies:         /storage/media/movies
- Documentaries:  /storage/media/documentaries
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET
from dotenv import load_dotenv

load_dotenv()


MEDIA_BASE = os.getenv("RC_MEDIA_BASE", "/storage/media")
DEFAULT_MOVIES_DIR = f"{MEDIA_BASE}/movies"
DEFAULT_DOCS_DIR = f"{MEDIA_BASE}/documentaries"
DEFAULT_MOVE_DEST = f"{MEDIA_BASE}/working/movies"


VIDEO_EXTS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
    ".webm",
    ".iso",
}

INTERMEDIATE_FILES = {"metadata.json", "tags.xml"}
REQUIRED_FILES = {"movie.nfo", "poster.jpg"}


def _which_ok(bin_name: str) -> bool:
    from shutil import which

    return which(bin_name) is not None


def _run_json(cmd: List[str], timeout: int) -> Dict[str, Any]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr.strip()}")
    return json.loads(p.stdout)


def _truncate(s: str, width: int) -> str:
    s = s or ""
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width <= 1:
        return s[:width]
    return s[: width - 1] + "…"


def _issue_bucket(issue: str) -> str:
    # bucket by prefix before '(' if present
    if not issue:
        return "unknown"
    i = issue.strip()
    if "(" in i:
        return i.split("(", 1)[0]
    return i


def _folder_candidates(root: str) -> List[str]:
    try:
        entries = os.listdir(root)
    except FileNotFoundError:
        return []
    out: List[str] = []
    for name in entries:
        p = os.path.join(root, name)
        if os.path.isdir(p) and not os.path.islink(p):
            out.append(p)
    out.sort()
    return out


def _list_files(folder: str) -> List[str]:
    try:
        return os.listdir(folder)
    except Exception:
        return []


def _is_video_file(name: str) -> bool:
    _, ext = os.path.splitext(name)
    return ext.lower() in VIDEO_EXTS


def _video_files(folder: str) -> List[str]:
    files = _list_files(folder)
    vids = [f for f in files if _is_video_file(f)]
    vids.sort()
    return vids


def _mkv_files(folder: str) -> List[str]:
    files = _list_files(folder)
    mkvs = [f for f in files if f.lower().endswith(".mkv")]
    mkvs.sort()
    return mkvs


def _parse_movie_nfo(nfo_path: str) -> Tuple[bool, List[str], Optional[str]]:
    """
    Returns (ok, issues, title_year_string)
    """
    issues: List[str] = []
    try:
        tree = ET.parse(nfo_path)
        root = tree.getroot()
    except Exception as e:
        return False, [f"nfo_parse_error({e})"], None

    if root.tag != "movie":
        issues.append(f"nfo_root_not_movie(tag={root.tag})")

    def get_text(tag: str) -> Optional[str]:
        el = root.find(tag)
        if el is None or el.text is None:
            return None
        t = el.text.strip()
        return t if t else None

    title = get_text("title")
    year = get_text("year")
    imdbid = get_text("imdbid")
    tmdbid = get_text("tmdbid")

    missing = [t for t, v in (("title", title), ("year", year), ("imdbid", imdbid), ("tmdbid", tmdbid)) if not v]
    if missing:
        issues.append("nfo_missing_fields(" + ",".join(missing) + ")")

    # If plot exists but is very short, flag (often placeholder)
    plot = get_text("plot")
    if plot is not None and len(plot) < 20:
        issues.append("nfo_plot_too_short")

    label = None
    if title and year:
        label = f"{title} ({year})"
    elif title:
        label = title

    ok = len([i for i in issues if i.startswith("nfo_parse_error") or i.startswith("nfo_root_not_movie") or i.startswith("nfo_missing_fields")]) == 0
    return ok, issues, label


def _mkv_deep_check(mkv_path: str) -> List[str]:
    """
    Uses mkvmerge -J to identify obvious "not cleaned" issues.
    """
    issues: List[str] = []
    try:
        info = _run_json(["mkvmerge", "-J", mkv_path], timeout=60)
    except Exception as e:
        return [f"mkvmerge_error({e})"]

    tracks = info.get("tracks", []) or []
    v = [t for t in tracks if t.get("type") == "video"]
    a = [t for t in tracks if t.get("type") == "audio"]
    s = [t for t in tracks if t.get("type") == "subtitles"]
    attachments = info.get("attachments", []) or []
    tags = info.get("tags", []) or []
    chapters = info.get("chapters", []) or []

    if len(v) != 1:
        issues.append(f"mkv_video_tracks({len(v)})")
    if len(a) == 0:
        issues.append("mkv_no_audio")
    if len(a) > 1:
        issues.append(f"mkv_multiple_audio({len(a)})")
    if len(s) > 1:
        issues.append(f"mkv_multiple_subs({len(s)})")
    if attachments:
        issues.append(f"mkv_attachments({len(attachments)})")
    if tags:
        issues.append(f"mkv_global_tags({len(tags)})")
    if chapters:
        issues.append(f"mkv_chapters({len(chapters)})")

    return issues


@dataclass
class FolderAudit:
    root_label: str
    folder_path: str
    folder_name: str
    video_files: List[str]
    mkv_files: List[str]
    issues: List[str]
    nfo_label: Optional[str]


def audit_root(root: str, root_label: str, deep: bool, include_nonvideo_folders: bool) -> Tuple[List[FolderAudit], List[str]]:
    problems: List[str] = []
    results: List[FolderAudit] = []

    if not os.path.isdir(root):
        problems.append(f"[WARN] {root_label}: root does not exist: {root}")
        return results, problems

    for folder in _folder_candidates(root):
        vids = _video_files(folder)
        mkvs = _mkv_files(folder)

        # Scope: ignore folders with no video unless explicitly requested
        if not vids and not include_nonvideo_folders:
            continue

        issues: List[str] = []
        nfo_label: Optional[str] = None

        # Video but not MKV (mp4/avi/etc)
        if vids and not mkvs:
            non_mkv = [v for v in vids if not v.lower().endswith(".mkv")]
            if non_mkv:
                issues.append("video_but_not_mkv(" + ",".join(sorted({os.path.splitext(v)[1].lower() for v in non_mkv})) + ")")
            else:
                issues.append("video_but_not_mkv")

        # Multiple video files / multiple MKVs (ambiguous)
        if len(vids) > 1:
            issues.append(f"multiple_video_files({len(vids)})")
        if len(mkvs) > 1:
            issues.append(f"multiple_mkvs({len(mkvs)})")

        # Missing required companion files (only meaningful if there is at least one MKV)
        if mkvs:
            present = set(_list_files(folder))
            missing = sorted([f for f in REQUIRED_FILES if f not in present])
            if missing:
                issues.append("missing_required(" + ",".join(missing) + ")")

            leftovers = sorted([f for f in INTERMEDIATE_FILES if f in present])
            if leftovers:
                issues.append("unexpected_intermediate(" + ",".join(leftovers) + ")")

            # NFO validation (cheap)
            nfo_path = os.path.join(folder, "movie.nfo")
            if os.path.isfile(nfo_path):
                ok, nfo_issues, label = _parse_movie_nfo(nfo_path)
                nfo_label = label
                if not ok:
                    issues.extend(nfo_issues)
            else:
                issues.append("missing_movie_nfo")

            # Optional deep MKV structure check (expensive)
            if deep and len(mkvs) == 1:
                mkv_path = os.path.join(folder, mkvs[0])
                issues.extend(_mkv_deep_check(mkv_path))

        # Record only folders with issues (unless include_nonvideo_folders)
        if issues:
            results.append(
                FolderAudit(
                    root_label=root_label,
                    folder_path=folder,
                    folder_name=os.path.basename(folder),
                    video_files=vids,
                    mkv_files=mkvs,
                    issues=issues,
                    nfo_label=nfo_label,
                )
            )

    return results, problems


def _unique_dest_path(dest_root: str, folder_name: str) -> str:
    """
    Pick a destination folder path that doesn't already exist.
    If dest_root/folder_name exists, append _DUP2, _DUP3, ...
    """
    base = os.path.join(dest_root, folder_name)
    if not os.path.exists(base):
        return base
    n = 2
    while True:
        cand = f"{base}_DUP{n}"
        if not os.path.exists(cand):
            return cand
        n += 1


def move_bad_folders(results: List[FolderAudit], dest_root: str) -> Tuple[int, List[str]]:
    """
    Move audited folders that have issues into dest_root.
    Returns (moved_count, errors)
    """
    errors: List[str] = []
    moved = 0
    os.makedirs(dest_root, exist_ok=True)

    # deterministic order
    for r in sorted(results, key=lambda x: (x.root_label, x.folder_name.lower(), x.folder_path)):
        src = r.folder_path
        if not os.path.isdir(src):
            errors.append(f"[SKIP] missing source folder: {src}")
            continue
        dst = _unique_dest_path(dest_root, r.folder_name)
        try:
            shutil.move(src, dst)
            moved += 1
            print(f"[MOVE] {r.root_label}: {src} -> {dst}")
        except Exception as e:
            errors.append(f"[ERROR] failed to move {src} -> {dst}: {e}")
    return moved, errors


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Audit existing movie/documentary libraries "
                    "(read-only by default; --move-bad relocates flagged folders).")
    ap.add_argument("--movies-dir", default=DEFAULT_MOVIES_DIR, help=f"Movies root (default: {DEFAULT_MOVIES_DIR})")
    ap.add_argument("--docs-dir", default=DEFAULT_DOCS_DIR, help=f"Documentaries root (default: {DEFAULT_DOCS_DIR})")
    ap.add_argument("--deep", action="store_true", help="Also run mkvmerge -J checks on single-MKV folders.")
    ap.add_argument(
        "--include-nonvideo-folders",
        action="store_true",
        help="Include folders with no video files (normally skipped).",
    )
    ap.add_argument("--limit", type=int, default=200, help="Max folders to print per root (default: 200)")
    ap.add_argument(
        "--show-videos",
        action="store_true",
        help="Show video filenames for each folder (otherwise hidden).",
    )
    ap.add_argument(
        "--move-bad",
        action="store_true",
        help=f"DESTRUCTIVE: move folders with issues to --move-dest (default: {DEFAULT_MOVE_DEST}).",
    )
    ap.add_argument(
        "--move-dest",
        default=DEFAULT_MOVE_DEST,
        help=f"Destination root for --move-bad (default: {DEFAULT_MOVE_DEST}).",
    )
    args = ap.parse_args()

    if args.deep and not _which_ok("mkvmerge"):
        print("ERROR: --deep requires mkvmerge in PATH")
        return 2

    print("=== Existing Library Audit ===")
    print(f"Movies root:        {args.movies_dir}")
    print(f"Documentaries root: {args.docs_dir}")
    print(f"Deep MKV checks:    {'YES' if args.deep else 'NO'}")
    print(f"Move bad folders:   {'YES' if args.move_bad else 'NO'}")
    if args.move_bad:
        print(f"Move destination:   {args.move_dest}")
    print()

    all_results: List[FolderAudit] = []
    for root, label in ((args.movies_dir, "MOVIES"), (args.docs_dir, "DOCS")):
        results, problems = audit_root(root, label, deep=args.deep, include_nonvideo_folders=args.include_nonvideo_folders)
        for p in problems:
            print(p)

        results.sort(key=lambda r: (r.folder_name.lower(), r.folder_path))
        all_results.extend(results)

        print(f"=== {label}: folders with issues ===")
        print(f"Count: {len(results)}")
        if not results:
            print("(none)")
            print()
            continue

        # Issue-type counts (top-level buckets)
        counts: Dict[str, int] = {}
        for r in results:
            for iss in r.issues:
                b = _issue_bucket(iss)
                counts[b] = counts.get(b, 0) + 1
        print("Issue counts:")
        for k in sorted(counts.keys(), key=lambda x: (-counts[x], x)):
            print(f"  - {k}: {counts[k]}")
        print()

        # Print up to limit in a scan-friendly table
        shown = results[: max(0, int(args.limit))]

        w_title = 42
        w_folder = 38
        w_issues = 44
        header = f"{'TITLE':<{w_title}}  {'FOLDER':<{w_folder}}  {'ISSUES':<{w_issues}}"
        print(header)
        print("-" * len(header))
        for r in shown:
            title = r.nfo_label or r.folder_name
            issues = ", ".join(r.issues)
            print(f"{_truncate(title, w_title):<{w_title}}  {_truncate(r.folder_name, w_folder):<{w_folder}}  {_truncate(issues, w_issues):<{w_issues}}")
            if args.show_videos and r.video_files:
                print(f"  videos: {', '.join(r.video_files)}")
        if len(shown) < len(results):
            print(f"... truncated: showing {len(shown)}/{len(results)} (use --limit to adjust)")
        print()

    if args.move_bad:
        print("=== Moving folders with issues ===")
        moved, errs = move_bad_folders(all_results, args.move_dest)
        print(f"Moved: {moved}")
        if errs:
            print(f"Move errors: {len(errs)}")
            for e in errs[:50]:
                print(e)
            if len(errs) > 50:
                print(f"... truncated: showing 50/{len(errs)} errors")
        else:
            print("Move errors: 0")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

