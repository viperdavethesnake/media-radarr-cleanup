#!/usr/bin/env python3
"""
Regenerate movie.nfo, poster.jpg, and fanart.jpg from TMDB for all movies
and documentaries. Uses MKV embedded tags as the authoritative source of
movie identity (TMDB ID / IMDB ID).

Existing NFOs are backed up before deletion. Posters are always refreshed.
fanart.jpg (backdrop) is downloaded fresh — it has never been stored locally.

Usage:
  python3 regenerate_nfos.py --audit                  # Dry-run report only
  python3 regenerate_nfos.py --run                    # Regenerate everything
  python3 regenerate_nfos.py --run --workers 12
  python3 regenerate_nfos.py --run --scan-dir /storage/media/movies
  python3 regenerate_nfos.py --audit --limit 10       # Quick test on 10 folders
  python3 regenerate_nfos.py --audit --json out.json  # Export results to JSON
  python3 regenerate_nfos.py --audit --csv  out.csv   # Export results to CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────────────

DEFAULT_SCAN_DIRS = [
    "/storage/media/movies",
    "/storage/media/documentaries",
]
SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
LOG_DIR = Path(SERVARR_DIR) / "logs"
BACKUP_DIR = Path(SERVARR_DIR) / "nfo_backups"
MAX_WORKERS = 8

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts"}

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_BASE    = "https://api.themoviedb.org/3"
TMDB_IMG     = "https://image.tmdb.org/t/p/original"
TMDB_THUMB   = "https://image.tmdb.org/t/p/w185"

shutdown_requested = False


# ─── Result dataclass ───────────────────────────────────────────────────────

@dataclass
class FolderResult:
    folder: str
    scan_dir: str
    status: str          # OK, READY, SKIP, FAIL
    tmdb_id: str = ""
    imdb_id: str = ""
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ─── Logging ─────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts   = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{ts} {msg}"
    print(line)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "regenerate_nfos.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _signal_handler(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    log("⚠️  Interrupt received — finishing current workers then stopping.")


signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ─── TMDB helpers ────────────────────────────────────────────────────────────

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _tmdb_get(path: str, params: Optional[dict] = None, retries: int = 3) -> dict:
    """Rate-limited TMDB GET with retry on 429."""
    session = _get_session()
    url = f"{TMDB_BASE}{path}"
    p   = {"api_key": TMDB_API_KEY, **(params or {})}
    for attempt in range(retries):
        resp = session.get(url, params=p, timeout=30)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "2"))
            log(f"  [RATE] 429 on {path}, sleeping {retry_after}s")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"TMDB request failed after {retries} retries: {path}")


def fetch_tmdb_by_tmdbid(tmdb_id: str) -> dict:
    return _tmdb_get(
        f"/movie/{tmdb_id}",
        {"append_to_response": "credits,release_dates,belongs_to_collection"},
    )


def fetch_tmdb_by_imdbid(imdb_id: str) -> dict:
    found = _tmdb_get(f"/find/{imdb_id}", {"external_source": "imdb_id"})
    results = found.get("movie_results", [])
    if not results:
        raise ValueError(f"No TMDB movie found for IMDB {imdb_id}")
    tmdb_id = results[0]["id"]
    return fetch_tmdb_by_tmdbid(str(tmdb_id))


def get_us_certification(meta: dict) -> str:
    """Extract US MPAA certification from release_dates."""
    for entry in meta.get("release_dates", {}).get("results", []):
        if entry.get("iso_3166_1") == "US":
            for rd in entry.get("release_dates", []):
                cert = rd.get("certification", "").strip()
                if cert:
                    return cert
    return ""


# ─── MKV tag reader ──────────────────────────────────────────────────────────

def read_mkv_tags(mkv_path: str) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", mkv_path],
        capture_output=True, text=True, timeout=30,
    )
    data = json.loads(result.stdout)
    return data.get("format", {}).get("tags", {})


# ─── NFO writer ──────────────────────────────────────────────────────────────

def _xml_sub(parent: Element, tag: str, text: Optional[str]) -> None:
    """Append a child element with text content to *parent*."""
    if text:
        el = SubElement(parent, tag)
        el.text = str(text).strip()


def build_nfo(meta: dict, imdb_id: str) -> Element:
    root = Element("movie")

    title      = (meta.get("title") or "").strip()
    orig       = (meta.get("original_title") or "").strip()
    year       = (meta.get("release_date") or "")[:4]
    released   = meta.get("release_date", "")
    overview   = meta.get("overview", "")
    tagline    = meta.get("tagline", "")
    runtime    = str(meta.get("runtime") or "")
    rating     = str(round(meta.get("vote_average", 0), 3))
    tmdb_id    = str(meta.get("id", ""))
    mpaa       = get_us_certification(meta)
    collection = meta.get("belongs_to_collection") or {}

    _xml_sub(root, "title",         title)
    _xml_sub(root, "originaltitle", orig)
    _xml_sub(root, "sorttitle",     title)
    _xml_sub(root, "year",          year)
    _xml_sub(root, "premiered",     released)
    _xml_sub(root, "releasedate",   released)
    _xml_sub(root, "dateadded",     time.strftime("%Y-%m-%d %H:%M:%S"))
    _xml_sub(root, "plot",          overview)
    _xml_sub(root, "tagline",       tagline)
    _xml_sub(root, "runtime",       runtime)
    _xml_sub(root, "mpaa",          mpaa)
    _xml_sub(root, "rating",        rating)

    # Provider IDs — modern <uniqueid> format (primary) + legacy tags (compat)
    _xml_sub(root, "tmdbid",        tmdb_id)
    _xml_sub(root, "imdbid",        imdb_id)
    if tmdb_id:
        uid = SubElement(root, "uniqueid", type="tmdb", default="true")
        uid.text = tmdb_id
    if imdb_id:
        uid = SubElement(root, "uniqueid", type="imdb")
        uid.text = imdb_id

    for genre in meta.get("genres", []):
        _xml_sub(root, "genre", genre.get("name"))

    for company in meta.get("production_companies", []):
        _xml_sub(root, "studio", company.get("name"))

    for country in meta.get("production_countries", []):
        _xml_sub(root, "country", country.get("name"))

    credits = meta.get("credits", {})

    for person in credits.get("crew", []):
        if person.get("job") == "Director":
            _xml_sub(root, "director", person.get("name"))

    for person in credits.get("crew", []):
        if person.get("job") == "Writer":
            _xml_sub(root, "credits", person.get("name"))

    for i, actor in enumerate(credits.get("cast", [])[:15]):
        el = SubElement(root, "actor")
        _xml_sub(el, "name",  actor.get("name"))
        _xml_sub(el, "role",  actor.get("character"))
        _xml_sub(el, "order", str(i))
        if actor.get("profile_path"):
            _xml_sub(el, "thumb", f"{TMDB_THUMB}{actor['profile_path']}")

    # Collection / set — only when TMDB confirms membership
    if collection:
        col_id = str(collection.get("id", ""))
        set_el = SubElement(root, "set")
        _xml_sub(set_el, "name",     collection.get("name"))
        _xml_sub(set_el, "overview", collection.get("overview", ""))
        # Jellyfin reads collectionnumber and uniqueid type="tmdbcol" (NOT setid)
        _xml_sub(root, "collectionnumber", col_id)
        if col_id:
            uid = SubElement(root, "uniqueid", type="tmdbcol")
            uid.text = col_id

    indent(root, space="  ")
    return root


def write_nfo(root: Element, dest: Path) -> None:
    tree = ElementTree(root)
    tree.write(str(dest), encoding="utf-8", xml_declaration=True)


# ─── Image downloader ────────────────────────────────────────────────────────

def download_image(url: str, dest: Path) -> None:
    session = _get_session()
    resp = session.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(65536):
            f.write(chunk)


# ─── Per-folder processor ────────────────────────────────────────────────────

def find_mkv(folder: Path) -> tuple[Optional[Path], list[str]]:
    """Return (mkv_path, warnings).  Skips symlinks.  Warns on non-MKV video
    files and on multiple MKVs (picks the first alphabetically)."""
    mkvs: list[Path] = []
    warnings: list[str] = []

    for f in sorted(folder.iterdir()):
        if f.is_symlink():
            continue
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext == ".mkv":
            mkvs.append(f)
        elif ext in VIDEO_EXTS:
            warnings.append(f"Non-MKV video file present: {f.name}")

    if not mkvs:
        return None, warnings

    if len(mkvs) > 1:
        warnings.append(f"Multiple MKVs found ({len(mkvs)}), using: {mkvs[0].name}")

    return mkvs[0], warnings


def process_folder(folder: Path, scan_dir: str, dry_run: bool) -> FolderResult:
    name   = folder.name
    result = FolderResult(folder=name, scan_dir=scan_dir, status="OK")

    try:
        mkv, mkv_warnings = find_mkv(folder)
        result.warnings.extend(mkv_warnings)

        if not mkv:
            result.status = "SKIP"
            result.issues.append("No MKV found")
            return result

        tags    = read_mkv_tags(str(mkv))
        tmdb_id = tags.get("TMDB", "").strip()
        imdb_id = tags.get("IMDB", "").strip()

        if not tmdb_id and not imdb_id:
            result.status = "FAIL"
            result.issues.append("MKV has no TMDB or IMDB tag — cannot identify")
            return result

        if dry_run:
            result.tmdb_id = tmdb_id
            result.imdb_id = imdb_id
            result.status  = "READY"
            return result

        # ── Fetch metadata ──
        if tmdb_id:
            meta    = fetch_tmdb_by_tmdbid(tmdb_id)
            imdb_id = imdb_id or meta.get("imdb_id", "")
        else:
            meta    = fetch_tmdb_by_imdbid(imdb_id)
            tmdb_id = str(meta.get("id", ""))

        # ── Backup + delete existing NFO ──
        nfo_path = folder / "movie.nfo"
        if nfo_path.exists():
            backup_root = BACKUP_DIR / Path(scan_dir).name / name
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(nfo_path, backup_root / "movie.nfo.bak")
            nfo_path.unlink()

        # ── Write new NFO ──
        nfo_root = build_nfo(meta, imdb_id)
        write_nfo(nfo_root, nfo_path)

        # ── Refresh poster ──
        poster_url = meta.get("poster_path")
        if poster_url:
            download_image(f"{TMDB_IMG}{poster_url}", folder / "poster.jpg")
        else:
            result.warnings.append("No poster available on TMDB")

        # ── Download fanart (backdrop) ──
        backdrop_url = meta.get("backdrop_path")
        if backdrop_url:
            download_image(f"{TMDB_IMG}{backdrop_url}", folder / "fanart.jpg")
        else:
            result.warnings.append("No backdrop available on TMDB")

        result.status  = "OK"
        result.tmdb_id = tmdb_id
        result.imdb_id = imdb_id

    except Exception as e:
        result.status = "FAIL"
        result.issues.append(str(e))
        log(f"  ❌ {name}: {e}\n{traceback.format_exc()}")

    return result


# ─── Export helpers ──────────────────────────────────────────────────────────

def write_json(results: list[FolderResult], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, indent=2, ensure_ascii=False)
    log(f"  JSON written → {path}")


def write_csv(results: list[FolderResult], path: str) -> None:
    fields = ["folder", "scan_dir", "status", "tmdb_id", "imdb_id", "issues", "warnings"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            row = asdict(r)
            row["issues"]   = " | ".join(row["issues"])
            row["warnings"] = " | ".join(row["warnings"])
            writer.writerow(row)
    log(f"  CSV written → {path}")


# ─── Main ────────────────────────────────────────────────────────────────────

def collect_folders(scan_dirs: list[str]) -> list[tuple[Path, str]]:
    """Return list of (folder_path, scan_dir) tuples.  Skips symlinks."""
    folders = []
    for d in scan_dirs:
        base = Path(d)
        if not base.is_dir():
            log(f"  ⚠️  Scan dir not found: {d}")
            continue
        for entry in sorted(base.iterdir()):
            if entry.is_symlink():
                continue
            if entry.is_dir():
                folders.append((entry, d))
    return folders


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate NFOs and artwork from TMDB",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true",
                      help="Dry-run: report readiness without making changes")
    mode.add_argument("--run",   action="store_true",
                      help="Delete old NFOs and regenerate everything")

    parser.add_argument("--scan-dir", action="append", dest="scan_dirs",
                        metavar="DIR",
                        help="Directory to scan (repeatable; default: movies + docs)")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS,
                        help=f"Parallel workers (default: {MAX_WORKERS})")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="Process only the first N folders (0 = all)")
    parser.add_argument("--json", metavar="PATH", dest="json_path",
                        help="Write results to a JSON file")
    parser.add_argument("--csv",  metavar="PATH", dest="csv_path",
                        help="Write results to a CSV file")

    args      = parser.parse_args()
    dry_run   = args.audit
    scan_dirs = args.scan_dirs or DEFAULT_SCAN_DIRS

    if not TMDB_API_KEY:
        log("❌  TMDB_API_KEY not set — check your .env file")
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    folders = collect_folders(scan_dirs)
    if args.limit > 0:
        folders = folders[:args.limit]

    mode_label = "AUDIT" if dry_run else "RUN"
    log(f"{'─'*60}")
    log(f"Mode       : {mode_label}")
    log(f"Directories: {', '.join(scan_dirs)}")
    log(f"Folders    : {len(folders)}")
    log(f"Workers    : {args.workers}")
    if args.limit > 0:
        log(f"Limit      : {args.limit}")
    if not dry_run:
        log(f"NFO backup : {BACKUP_DIR.resolve()}")
    log(f"{'─'*60}")

    results: list[FolderResult] = []
    completed = 0
    total     = len(folders)
    start     = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_folder, f, sd, dry_run): (f, sd)
            for f, sd in folders
        }
        try:
            for future in as_completed(futures):
                if shutdown_requested:
                    log("⚠️  Shutdown requested — cancelling remaining work.")
                    for f in futures:
                        f.cancel()
                    break
                completed += 1
                try:
                    r = future.result(timeout=120)
                    results.append(r)
                    icon = {"OK": "✅", "READY": "✅", "SKIP": "⏭ ", "FAIL": "❌"}.get(r.status, "❓")
                    pct  = completed / total * 100
                    log(f"  {icon} [{completed}/{total} {pct:.0f}%] {r.folder}")
                    for w in r.warnings:
                        log(f"      ⚠️  {w}")
                    for i in r.issues:
                        log(f"      ❌ {i}")
                except Exception as e:
                    # Record the failure so it appears in the summary/exports
                    # and the exit code — a bare log line here silently drops
                    # the folder from the report (errors must accumulate).
                    folder, scan_dir = futures[future]
                    results.append(FolderResult(
                        folder=os.path.basename(folder), scan_dir=scan_dir,
                        status="FAIL", issues=[f"worker error: {e}"]))
                    log(f"  💥 Worker error in {os.path.basename(folder)}: {e}")
        except KeyboardInterrupt:
            log("⚠️  Interrupted.")

    # ── Summary ──
    elapsed = time.time() - start
    ok    = sum(1 for r in results if r.status in ("OK", "READY"))
    fails = [r for r in results if r.status == "FAIL"]
    skips = [r for r in results if r.status == "SKIP"]
    warns = sum(1 for r in results if r.warnings)

    log(f"\n{'═'*60}")
    log(f"{'AUDIT SUMMARY' if dry_run else 'RUN SUMMARY'}")
    log(f"{'═'*60}")
    log(f"  Total   : {len(results)}")
    log(f"  {'Ready' if dry_run else 'Processed'}: {ok}")
    log(f"  Warnings: {warns}")
    log(f"  Failed  : {len(fails)}")
    log(f"  Skipped : {len(skips)}")
    log(f"  Time    : {elapsed:.1f}s")

    if fails:
        log(f"\nFailed folders:")
        for r in fails:
            log(f"  • {r.folder}: {'; '.join(r.issues)}")

    if skips:
        log(f"\nSkipped (no MKV):")
        for r in skips:
            log(f"  • {r.folder}")

    if dry_run and fails:
        log("\n⚠️  Resolve the above before running with --run")

    # ── Export ──
    if args.json_path:
        write_json(results, args.json_path)
    if args.csv_path:
        write_csv(results, args.csv_path)

    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
