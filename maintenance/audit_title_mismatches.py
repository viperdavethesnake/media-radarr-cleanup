#!/usr/bin/env python3
"""
Audit movie folders for title mismatches between folder name and movie.nfo <title>.

If a source file had the wrong IMDb ID, batch_cleaner.py would rename the folder
AND write an NFO — so both would reflect the wrong title. This script catches the
case where the folder name still contains the *original* title but the NFO disagrees.

Usage:
    python3 maintenance/audit_title_mismatches.py /storage/media/movies
    python3 maintenance/audit_title_mismatches.py /storage/media/movies --csv mismatches.csv
"""

import os
import re
import sys
import csv
import argparse
from xml.etree.ElementTree import parse as et_parse
from dotenv import load_dotenv

load_dotenv()

def normalize(s):
    """Strip punctuation, underscores, lowercase for fuzzy comparison."""
    s = s.lower()
    s = re.sub(r'[_\s]+', ' ', s)
    s = re.sub(r'[^a-z0-9 ]', '', s)
    return s.strip()

def folder_title(folder_name):
    """Extract title portion from folder name like 'Movie_Title_(2001)' or 'Movie_Title_(2001)_tt1234567'."""
    # Strip trailing IMDb ID
    name = re.sub(r'_?tt\d{6,8}$', '', folder_name)
    # Strip trailing _(YYYY)
    name = re.sub(r'_?\(\d{4}\)$', '', name).strip('_')
    return name

def read_nfo_title(nfo_path):
    try:
        tree = et_parse(nfo_path)
        root = tree.getroot()
        el = root.find('title')
        return el.text.strip() if el is not None and el.text else None
    except Exception:
        return None

def read_nfo_imdbid(nfo_path):
    try:
        tree = et_parse(nfo_path)
        root = tree.getroot()
        el = root.find('imdbid')
        return el.text.strip() if el is not None and el.text else None
    except Exception:
        return None

def check_folder(folder_path):
    folder_name = os.path.basename(folder_path)
    nfo_path = os.path.join(folder_path, 'movie.nfo')

    if not os.path.isfile(nfo_path):
        return None  # Skip — verify_movies.py already catches missing NFOs

    nfo_title = read_nfo_title(nfo_path)
    imdbid = read_nfo_imdbid(nfo_path)

    if not nfo_title:
        return {
            'folder': folder_name,
            'folder_title': folder_title(folder_name),
            'nfo_title': '(MISSING)',
            'imdbid': imdbid or '(MISSING)',
            'match': False,
            'note': 'NFO has no <title>',
        }

    ft = normalize(folder_title(folder_name))
    nt = normalize(nfo_title)

    # Check if NFO title words appear in folder title (handles subtitle truncation etc.)
    # A match requires at least 60% of significant words to overlap
    ft_words = set(ft.split())
    nt_words = set(nt.split())
    if not nt_words:
        match = False
    else:
        overlap = ft_words & nt_words
        match = len(overlap) / len(nt_words) >= 0.6

    if match:
        return None  # No problem

    return {
        'folder': folder_name,
        'folder_title': folder_title(folder_name),
        'nfo_title': nfo_title,
        'imdbid': imdbid or '(MISSING)',
        'match': False,
        'note': 'Folder name does not match NFO title',
    }

def main():
    parser = argparse.ArgumentParser(description='Audit movie folders for title mismatches')
    parser.add_argument('scan_dir', nargs='?', default=os.getenv('RC_VERIFY_SCAN_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'movies'),
                        help='Directory containing processed movie folders')
    parser.add_argument('--csv', metavar='FILE', help='Write results to CSV')
    args = parser.parse_args()

    scan_dir = args.scan_dir
    if not os.path.isdir(scan_dir):
        print(f"ERROR: Not a directory: {scan_dir}")
        sys.exit(1)

    print(f"Scanning: {scan_dir}\n")

    folders = sorted(
        f for f in os.listdir(scan_dir)
        if os.path.isdir(os.path.join(scan_dir, f)) and not os.path.islink(os.path.join(scan_dir, f))
    )

    mismatches = []
    for folder in folders:
        result = check_folder(os.path.join(scan_dir, folder))
        if result:
            mismatches.append(result)

    total = len(folders)
    if not mismatches:
        print(f"✅ All {total} folders look clean — no title mismatches found.")
        return

    print(f"⚠️  Found {len(mismatches)} potential mismatch(es) out of {total} folders:\n")
    print(f"{'Folder name':50}  {'NFO title':40}  IMDb ID")
    print("-" * 110)
    for m in mismatches:
        folder_display = m['folder'][:50]
        nfo_display = m['nfo_title'][:40]
        print(f"{folder_display:50}  {nfo_display:40}  {m['imdbid']}")
        if m['note'] != 'Folder name does not match NFO title':
            print(f"  ^ {m['note']}")

    if args.csv:
        with open(args.csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['folder', 'folder_title', 'nfo_title', 'imdbid', 'note'])
            writer.writeheader()
            writer.writerows(mismatches)
        print(f"\nCSV written to: {args.csv}")

if __name__ == '__main__':
    main()
