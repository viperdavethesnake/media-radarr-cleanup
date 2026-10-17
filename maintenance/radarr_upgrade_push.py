#!/usr/bin/env python3
"""
Add movies to Radarr with the "Mine" quality profile so Radarr downloads
the best available version (Remux-2160p preferred, AV1 > h265 > h264).

Sources:
  - Net-new movies (not yet on gold library)
  - Upgrade movies (gold copy exists but new copy is better quality)

Both lists come from compare_movie_copies.py output.

Usage:
    python3 maintenance/radarr_upgrade_push.py             # preview (dry-run is the default)
    python3 maintenance/radarr_upgrade_push.py --run       # add to Radarr + search
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

from dotenv import load_dotenv

load_dotenv()

# Each site (192.168.33.x and 192.168.36.x) runs its own Radarr. The script
# talks to the one on the subnet it is running on, picked from the host's
# own IPs; RC_SITE=33|36 overrides when detection fails.
SITES = ("33", "36")


def _local_ipv4s() -> List[str]:
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True,
                             text=True, check=True).stdout
        return [a["local"] for i in json.loads(out) for a in i.get("addr_info", [])]
    except Exception:
        return [ai[4][0] for ai in
                socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]


def pick_site(local_ips: List[str], override: Optional[str]) -> str:
    if override:
        if override not in SITES:
            sys.exit(f"RC_SITE={override!r} is not one of {SITES}.")
        return override
    found = {s for s in SITES for ip in local_ips if ip.startswith(f"192.168.{s}.")}
    if len(found) != 1:
        sys.exit(f"Cannot tell which site this host is on (local IPs: {local_ips}); "
                 f"set RC_SITE to one of {SITES}.")
    return found.pop()


SITE        = pick_site(_local_ipv4s(), os.getenv("RC_SITE"))
RADARR_URL  = os.getenv(f"RADARR_URL_{SITE}", f"http://192.168.{SITE}.195:7878")
API_KEY     = os.getenv(f"RADARR_API_KEY_{SITE}", "")   # no hardcoded default — set in .env
PROFILE_ID  = int(os.getenv(f"RADARR_PROFILE_ID_{SITE}", "7"))   # "Mine"
ROOT_FOLDER = os.getenv(f"RADARR_ROOT_FOLDER_{SITE}", "/servarr/servarr/movies")
COMPARE_OUTPUT = "/tmp/compare_output.txt"


# ---------------------------------------------------------------------------
# Radarr API
# ---------------------------------------------------------------------------

def _req(method: str, path: str, body: Optional[Dict] = None) -> Any:
    url = f"{RADARR_URL}/api/v3{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "X-Api-Key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {e.read().decode(errors='ignore')[:300]}")


def radarr_get_all_imdb_ids() -> set:
    movies = _req("GET", "/movie")
    return {(m.get("imdbId") or "").lower() for m in movies if m.get("imdbId")}


def radarr_lookup(imdb_id: str) -> Optional[Dict]:
    results = _req("GET", f"/movie/lookup?term=imdb:{imdb_id}")
    return results[0] if results else None


def radarr_add(movie: Dict, search: bool, dry_run: bool) -> Optional[int]:
    payload = {
        "title":               movie.get("title", ""),
        "titleSlug":           movie.get("titleSlug", ""),
        "year":                movie.get("year", 0),
        "tmdbId":              movie.get("tmdbId"),
        "imdbId":              movie.get("imdbId", ""),
        "qualityProfileId":    PROFILE_ID,
        "rootFolderPath":      ROOT_FOLDER,
        "monitored":           True,
        "minimumAvailability": "released",
        "images":              movie.get("images", []),
        "addOptions":          {"searchForMovie": search},
    }
    if dry_run:
        return None
    result = _req("POST", "/movie", payload)
    return result.get("id")


# ---------------------------------------------------------------------------
# Parse compare output
# ---------------------------------------------------------------------------

def parse_all_imdb_ids(path: str) -> List[str]:
    """
    Pull every IMDb ID we care about:
      Section 1  — net-new movies (lines starting with "- imdb:tt...")
      Section 3  — KEEP NEW detail blocks (lines starting with "--- imdb:tt...")
    """
    ids: List[str] = []
    seen: set = set()
    low_conf_skipped = 0
    saw_detail_section = False

    with open(path) as f:
        in_new_only = False
        in_detail   = False
        current_imdb: Optional[str] = None

        for line in f:
            # Section 1: net-new
            if "=== 1) Movies in NEW that are NOT in EXISTING ===" in line:
                in_new_only = True
                continue
            if in_new_only:
                if line.startswith("==="):
                    in_new_only = False
                elif line.startswith("- imdb:"):
                    m = re.match(r"- imdb:(tt\d+)", line)
                    if m and m.group(1) not in seen:
                        ids.append(m.group(1))
                        seen.add(m.group(1))

            # Section 3: detailed comparison — KEEP NEW blocks
            if "=== 3) Movies present in BOTH" in line:
                in_detail = True
                saw_detail_section = True
                continue
            if in_detail:
                if line.startswith("--- "):
                    current_imdb = None
                    m = re.search(r"imdb:(tt\d+)", line)
                    if m:
                        current_imdb = m.group(1)
                elif "RECOMMEND: KEEP NEW" in line and current_imdb:
                    # Only push high-confidence upgrades: a Radarr add triggers
                    # an immediate download, so medium-confidence verdicts
                    # (small bitrate gaps, audio-only wins) stay manual.
                    cm = re.search(r"confidence=(\w+)", line)
                    if cm and cm.group(1) != "high":
                        low_conf_skipped += 1
                    elif current_imdb not in seen:
                        ids.append(current_imdb)
                        seen.add(current_imdb)
                    current_imdb = None

    if not saw_detail_section:
        print("WARNING: compare output has no Section 3 detail blocks — "
              "upgrade (KEEP NEW) candidates are only emitted when "
              "compare_movie_copies.py runs with --details. Net-new only.")
    if low_conf_skipped:
        print(f"NOTE: skipped {low_conf_skipped} KEEP-NEW recommendation(s) "
              f"below high confidence.")

    return ids


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Add net-new and upgrade movies to Radarr with the 'Mine' profile."
    )
    ap.add_argument("--run", action="store_true",
                    help="Actually add movies to Radarr and trigger searches. "
                         "Without this flag the script previews only (dry run).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Deprecated: dry run is now the default; this flag is a no-op.")
    ap.add_argument("--compare-output", default=COMPARE_OUTPUT,
                    help=f"Path to compare_movie_copies.py output (default: {COMPARE_OUTPUT})")
    args = ap.parse_args()

    if not API_KEY:
        sys.exit(f"RADARR_API_KEY_{SITE} is not set — add it to .env (see .env.example).")

    # Dry-run by default (maintenance convention: destructive ops require --run).
    # Adding a movie to Radarr triggers an immediate Usenet search + download.
    dry_run = not args.run
    if dry_run:
        print("=== DRY RUN — no changes will be made (pass --run to push) ===\n")

    imdb_ids = parse_all_imdb_ids(args.compare_output)
    print(f"Movies to add: {len(imdb_ids)}")
    print(f"Radarr:        {RADARR_URL}  (site {SITE})")
    print(f"Profile:       Mine (id={PROFILE_ID})")
    print(f"Root folder:   {ROOT_FOLDER}")
    print()

    print("Checking which are already in Radarr...")
    existing = radarr_get_all_imdb_ids()
    to_add   = [i for i in imdb_ids if i.lower() not in existing]
    skipped  = len(imdb_ids) - len(to_add)
    print(f"  Already in Radarr: {skipped}  →  will add: {len(to_add)}\n")

    added = 0
    failed = []

    for n, imdb in enumerate(to_add, 1):
        print(f"  [{n}/{len(to_add)}] {imdb} ", end="", flush=True)

        try:
            movie = radarr_lookup(imdb)
        except Exception as e:
            print(f"LOOKUP ERROR: {e}")
            failed.append((imdb, str(e)))
            continue

        if not movie:
            print("not found in TMDB, skipping")
            failed.append((imdb, "not found in TMDB lookup"))
            continue

        title = movie.get("title", "?")
        year  = movie.get("year", "")
        print(f"→ {title} ({year})", end=" ")

        try:
            rid = radarr_add(movie, search=True, dry_run=dry_run)
            if dry_run:
                print("[DRY-RUN would add + search]")
            else:
                print(f"added (id={rid})")
                added += 1
        except Exception as e:
            print(f"ADD ERROR: {e}")
            failed.append((imdb, str(e)))

        time.sleep(0.25)  # polite pacing

    print(f"""
=== Summary ===
  Requested:          {len(imdb_ids)}
  Already in Radarr:  {skipped}
  Added:              {added if not dry_run else f'0 (dry-run, would add {len(to_add)})'}
  Failed:             {len(failed)}
""")

    if failed:
        print("Failed:")
        for imdb, reason in failed:
            print(f"  {imdb}: {reason}")

    if not dry_run and added:
        print(f"Radarr is now searching. Monitor at: {RADARR_URL}/activity/queue")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
