#!/usr/bin/env python3
"""
Compare local movie library against one or more remote paths on the same SSH host.

Differences from compare_movie_copies.py:
  - Local side is always the NEW side (no --ssh-a)
  - No ffprobe/quality comparison, no recommendation, no plan file, no --apply
  - Local movies are classified as Movie or Documentary via NFO <genre> tags
  - Simpler output focused on "what do I have that remote doesn't"

Usage:
  python3 maintenance/local_vs_remote_compare.py \\
      --local /storage/media/movies \\
      --remote-host david@192.168.1.200 \\
      --remote-paths /mnt/media1/movies /mnt/media2/movies /mnt/media3/movies

Defaults (editable below) match the standard setup.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import re
import sys
from typing import Dict, List, Optional, Set, Tuple
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Add parent dir to path so we can import from compare_movie_copies
# ---------------------------------------------------------------------------
_MAINTENANCE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _MAINTENANCE_DIR)

from compare_movie_copies import (  # noqa: E402
    Shell,
    MovieEntry,
    _parse_movie_nfo,
    _read_mkv_imdb,
    _pick_main_mkv,
    _infer_title_year_from_folder,
    _extract_imdb_any,
    _make_key,
    _fmt_identity,
    index_library,
)

# ---------------------------------------------------------------------------
# Defaults (override via CLI args)
# ---------------------------------------------------------------------------
DEFAULT_LOCAL_PATH   = "/storage/media/movies"
DEFAULT_REMOTE_HOST  = "david@192.168.1.200"
DEFAULT_REMOTE_PATHS = [
    "/mnt/media1/movies",
    "/mnt/media2/movies",
    "/mnt/media3/movies",
]


# ---------------------------------------------------------------------------
# Genre / category helpers
# ---------------------------------------------------------------------------

def _parse_genres(folder: str, shell: Shell) -> List[str]:
    """Return list of genre strings from movie.nfo (or any .nfo) in folder."""
    nfo_path = folder + "/movie.nfo"
    txt = shell.read_text(nfo_path)
    if not txt:
        for f in shell.list_files(folder):
            if f.lower().endswith(".nfo"):
                txt = shell.read_text(folder + "/" + f)
                if txt:
                    break
    if not txt:
        return []

    genres: List[str] = []
    # Try XML parse first
    try:
        root = ET.fromstring(txt)
        for g in root.findall("genre"):
            if g.text:
                # genres may be comma-separated in one tag or one-per-tag
                for part in g.text.split(","):
                    part = part.strip()
                    if part:
                        genres.append(part)
    except Exception:
        # Regex fallback
        for m in re.finditer(r"<genre>(.*?)</genre>", txt, re.IGNORECASE | re.DOTALL):
            for part in m.group(1).split(","):
                part = part.strip()
                if part:
                    genres.append(part)
    return genres


def _classify(genres: List[str]) -> str:
    """Return 'Documentary' if any genre matches, else 'Movie'."""
    for g in genres:
        if "documentary" in g.lower():
            return "Documentary"
    return "Movie"


# ---------------------------------------------------------------------------
# Merge multiple remote indexes into one
# ---------------------------------------------------------------------------

def index_remote_multi(
    paths: List[str],
    ssh_host: str,
    threads: int = 10,
) -> Tuple[Dict[Tuple[str, str], List[MovieEntry]], List[str]]:
    """Index all remote paths and merge into a single key→entries dict."""
    shell = Shell(host=ssh_host)
    merged: Dict[Tuple[str, str], List[MovieEntry]] = {}
    all_problems: List[str] = []

    for path in paths:
        idx, problems = index_library(path, f"REMOTE({path})", shell, threads=threads)
        all_problems.extend(problems)
        for k, entries in idx.items():
            merged.setdefault(k, []).extend(entries)

    return merged, all_problems


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_CATEGORY_EMOJI = {
    "Documentary": "🎞 ",
    "Movie":       "🎬 ",
    "Unknown":     "❓ ",
}


def _truncate(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    return s[:width - 1] + "…"


def _local_only_table(
    only_local: List[Tuple[str, str]],
    local_index: Dict[Tuple[str, str], List[MovieEntry]],
    local_shell: Shell,
) -> None:
    """Print local-only movies with category classification."""
    rows = []
    for k in only_local:
        for e in local_index.get(k, []):
            # Skip non-movie directories (no MKV, no IMDb, no title+year → probably logs/, tmp/, etc.)
            if "no_mkv" in e.warnings and not e.imdb and not (e.title and e.year):
                continue
            genres = _parse_genres(e.folder, local_shell)
            cat = _classify(genres)
            genre_str = ", ".join(genres) if genres else "(no genre)"
            title = _fmt_identity(e)
            imdb = e.imdb or ""
            rows.append((cat, title, imdb, genre_str, os.path.basename(e.folder)))

    rows.sort(key=lambda r: (r[0], r[1].lower()))  # Documentary first (D < M)

    # Count by category
    cats: Dict[str, int] = {}
    for r in rows:
        cats[r[0]] = cats.get(r[0], 0) + 1
    summary = "  ".join(f"{c}: {n}" for c, n in sorted(cats.items()))
    print(f"Count: {len(rows)}  ({summary})")
    print()

    w_cat = 12
    w_title = 42
    w_imdb = 11
    header = f"  {'CATEGORY':<{w_cat}}  {'TITLE':<{w_title}}  {'IMDB':<{w_imdb}}  GENRES"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for cat, title, imdb, genre_str, _folder in rows:
        emoji = _CATEGORY_EMOJI.get(cat, "  ")
        print(
            f"  {emoji}{_truncate(cat, w_cat - 2):<{w_cat - 2}}  "
            f"{_truncate(title, w_title):<{w_title}}  "
            f"{_truncate(imdb, w_imdb):<{w_imdb}}  "
            f"{genre_str}"
        )


def _remote_only_table(
    only_remote: List[Tuple[str, str]],
    remote_index: Dict[Tuple[str, str], List[MovieEntry]],
) -> None:
    rows = []
    for k in only_remote:
        for e in remote_index.get(k, []):
            rows.append((_fmt_identity(e), e.imdb or "", os.path.basename(e.folder)))
    rows.sort(key=lambda r: r[0].lower())
    print(f"Count: {len(rows)}")
    print()
    for title, imdb, folder in rows:
        tag = f"  [{imdb}]" if imdb else ""
        print(f"  {title}{tag}")


def _both_table(
    both: List[Tuple[str, str]],
    local_index: Dict[Tuple[str, str], List[MovieEntry]],
    remote_index: Dict[Tuple[str, str], List[MovieEntry]],
    local_shell: Shell,
    show_genres: bool = True,
) -> None:
    rows = []
    for k in both:
        local_entries = local_index.get(k, [])
        remote_entries = remote_index.get(k, [])
        if not local_entries:
            continue
        e = local_entries[0]
        genres = _parse_genres(e.folder, local_shell) if show_genres else []
        cat = _classify(genres)
        title = _fmt_identity(e)
        imdb = e.imdb or (remote_entries[0].imdb if remote_entries else "")
        remote_folder = os.path.basename(remote_entries[0].folder) if remote_entries else "?"
        rows.append((cat, title, imdb, remote_folder))

    rows.sort(key=lambda r: (r[0], r[1].lower()))
    print(f"Count: {len(rows)}")
    print()

    w_cat = 12
    w_title = 42
    w_imdb = 11
    header = f"  {'CATEGORY':<{w_cat}}  {'TITLE':<{w_title}}  {'IMDB':<{w_imdb}}  REMOTE FOLDER"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for cat, title, imdb, remote_folder in rows:
        emoji = _CATEGORY_EMOJI.get(cat, "  ")
        print(
            f"  {emoji}{_truncate(cat, w_cat - 2):<{w_cat - 2}}  "
            f"{_truncate(title, w_title):<{w_title}}  "
            f"{_truncate(imdb, w_imdb):<{w_imdb}}  "
            f"{remote_folder}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Compare a local movie library against one or more remote paths on the same host.\n"
            "Classifies local movies as Movie or Documentary via NFO genre tags."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--local", default=DEFAULT_LOCAL_PATH,
        help=f"Local movie library path (default: {DEFAULT_LOCAL_PATH})",
    )
    ap.add_argument(
        "--remote-host", default=DEFAULT_REMOTE_HOST,
        help=f"SSH host for the remote server (default: {DEFAULT_REMOTE_HOST})",
    )
    ap.add_argument(
        "--remote-paths", nargs="+", default=DEFAULT_REMOTE_PATHS,
        metavar="PATH",
        help="One or more remote movie library paths to merge (default: media1/2/3)",
    )
    ap.add_argument(
        "--threads", type=int, default=10,
        help="Concurrent indexing threads per path (default: 10)",
    )
    ap.add_argument(
        "--show-remote-only", action="store_true",
        help="Also show movies on remote that are NOT in local library",
    )
    ap.add_argument(
        "--show-both", action="store_true",
        help="Show movies present in both local and remote (with category)",
    )
    ap.add_argument(
        "--strict-imdb", action="store_true",
        help="Only match movies by IMDb ID (skip title/year matches)",
    )
    args = ap.parse_args()

    local_shell = Shell(host=None)  # always local

    # ---- Sanity check local path ----
    if not os.path.isdir(args.local):
        print(f"ERROR: local path not found: {args.local}", file=sys.stderr)
        return 2

    print("=" * 60)
    print("  Local vs Remote Movie Comparison")
    print("=" * 60)
    print(f"  Local  : {args.local}")
    print(f"  Remote : {args.remote_host}")
    for p in args.remote_paths:
        print(f"           {p}")
    print()

    # ---- Index local ----
    print("Indexing local library...", flush=True)
    local_index, local_problems = index_library(
        args.local, "LOCAL", local_shell, threads=args.threads
    )
    for p in local_problems:
        print(f"  {p}")
    print(f"  Found {sum(len(v) for v in local_index.values())} local movies in {len(local_index)} unique keys")
    print()

    # ---- Index remote (all paths, merged) ----
    print(f"Indexing remote ({args.remote_host}) — {len(args.remote_paths)} path(s)...", flush=True)
    remote_index, remote_problems = index_remote_multi(
        args.remote_paths, args.remote_host, threads=args.threads
    )
    for p in remote_problems:
        print(f"  {p}")
    print(f"  Found {sum(len(v) for v in remote_index.values())} remote movies in {len(remote_index)} unique keys")
    print()

    if args.strict_imdb:
        local_index  = {k: v for k, v in local_index.items()  if k[0] == "imdb"}
        remote_index = {k: v for k, v in remote_index.items() if k[0] == "imdb"}

    local_keys  = set(local_index.keys())
    remote_keys = set(remote_index.keys())

    only_local  = sorted(local_keys  - remote_keys)
    only_remote = sorted(remote_keys - local_keys)
    both        = sorted(local_keys  & remote_keys)

    # ---- Section 1: Local-only (NEW) ----
    print("=" * 60)
    print("  Section 1: LOCAL ONLY — movies you have that remote doesn't")
    print("=" * 60)
    _local_only_table(only_local, local_index, local_shell)
    print()

    # ---- Section 2: Both (optional) ----
    if args.show_both:
        print("=" * 60)
        print("  Section 2: BOTH — present in local AND remote")
        print("=" * 60)
        _both_table(both, local_index, remote_index, local_shell)
        print()

    # ---- Section 3: Remote-only (optional) ----
    if args.show_remote_only:
        print("=" * 60)
        print("  Section 3: REMOTE ONLY — on remote but not in local library")
        print("=" * 60)
        _remote_only_table(only_remote, remote_index)
        print()

    # ---- Summary ----
    print("=" * 60)
    print("  Summary")
    print("=" * 60)
    print(f"  Local-only  (you have, remote doesn't): {len(only_local)}")
    print(f"  Both        (present on both sides):    {len(both)}")
    print(f"  Remote-only (remote has, you don't):    {len(only_remote)}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
