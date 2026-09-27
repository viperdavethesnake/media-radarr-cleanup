#!/usr/bin/env python3

import os
import random
import subprocess
import json
import re
import xml.etree.ElementTree as ET

from dotenv import load_dotenv

load_dotenv()

MOVIES_DIR = os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'movies')
NUM_SAMPLES = 100

def get_nfo_tags(nfo_path):
    tags = {}
    try:
        # Try parsing as XML first
        tree = ET.parse(nfo_path)
        root = tree.getroot()
        
        if root.tag == 'movie':
            tags['title'] = root.findtext('title')
            tags['year'] = root.findtext('year')
            tags['imdbid'] = root.findtext('imdbid') or root.findtext('id')
            tags['tmdbid'] = root.findtext('tmdbid')
            tags['plot'] = root.findtext('plot')
            tags['genre'] = root.findtext('genre')
            
            # Clean up None values
            tags = {k: v for k, v in tags.items() if v}
            return tags
    except ET.ParseError:
        pass
    except Exception as e:
        print(f"Error parsing NFO XML: {e}")

    # Fallback to regex if XML parsing fails or returns empty
    with open(nfo_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    if 'title' not in tags:
        match = re.search(r'<title>(.*?)</title>', content, re.IGNORECASE)
        if match: tags['title'] = match.group(1)
        
    if 'year' not in tags:
        match = re.search(r'<year>(\d{4})</year>', content, re.IGNORECASE)
        if match: tags['year'] = match.group(1)

    if 'imdbid' not in tags:
        match = re.search(r'<imdbid>(tt\d+)</imdbid>', content, re.IGNORECASE)
        if not match:
             match = re.search(r'(tt\d{7,9})', content, re.IGNORECASE)
        if match: tags['imdbid'] = match.group(1)

    return tags

def get_mkv_tags(mkv_path):
    try:
        cmd = ['mediainfo', '--Output=JSON', mkv_path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        
        media = data.get('media', {})
        tracks = media.get('track', [])
        
        # Find General track
        general_track = next((t for t in tracks if t.get('@type') == 'General'), None)
        
        if not general_track:
            return {}
            
        tags = {}
        
        # Direct fields
        if 'Title' in general_track:
            tags['Title'] = general_track['Title']
        if 'Movie' in general_track:
            tags['Movie'] = general_track['Movie']
            
        # Extra fields (where custom tags usually live)
        extra = general_track.get('extra', {})
        tags.update(extra)
        
        return tags
    except Exception as e:
        print(f"Error reading MKV tags: {e}")
        return {}

def check_movie(folder_path):
    print(f"Checking: {os.path.basename(folder_path)}")
    
    result = {
        'folder': os.path.basename(folder_path),
        'status': 'OK',
        'matches': 0,
        'mismatches': 0,
        'missing': 0,
        'details': []
    }

    nfo_path = os.path.join(folder_path, 'movie.nfo')
    if not os.path.exists(nfo_path):
        print("  [WARN] No movie.nfo found")
        result['status'] = 'NO_NFO'
        return result
        
    mkv_files = [f for f in os.listdir(folder_path) if f.lower().endswith('.mkv')]
    if not mkv_files:
        print("  [WARN] No MKV file found")
        result['status'] = 'NO_MKV'
        return result
    
    mkv_path = os.path.join(folder_path, mkv_files[0])
    
    nfo_tags = get_nfo_tags(nfo_path)
    mkv_tags = get_mkv_tags(mkv_path)
    
    # Normalize keys for comparison
    # NFO keys are lowercase: title, year, imdbid, tmdbid
    # MKV keys are mixed/uppercase: Title, YEAR, IMDB, TMDB
    
    # Comparison Logic
    matches = []
    mismatches = []
    missing = []
    
    # Map NFO keys to likely MKV tag keys
    key_map = {
        'title': ['Title', 'Movie', 'TITLE'],
        'year': ['YEAR', 'DATE_RELEASED', 'DATE'],
        'imdbid': ['IMDB', 'IMDB_ID'],
        'tmdbid': ['TMDB', 'TMDB_ID'],
        'plot': ['PLOT', 'DESCRIPTION'],
        'genre': ['GENRE']
    }
    
    for nfo_key, nfo_val in nfo_tags.items():
        if not nfo_val: continue
        if nfo_key not in key_map: continue # Skip keys we don't check
        
        found = False
        possible_mkv_keys = key_map.get(nfo_key, [])
        
        for mkv_key in possible_mkv_keys:
            if mkv_key in mkv_tags:
                mkv_val = mkv_tags[mkv_key]
                
                # Normalize values for comparison
                val1 = str(nfo_val).strip()
                val2 = str(mkv_val).strip()
                
                if val1 == val2:
                    matches.append(f"{nfo_key}={val1}")
                    found = True
                    break
                else:
                    # Check for partial matches or slight format differences
                    if nfo_key == 'year' and val1 in val2:
                         matches.append(f"{nfo_key}={val1} (in {val2})")
                         found = True
                         break
                    
                    # Title often has extra info in MKV or NFO
                    if nfo_key == 'title' and (val1 in val2 or val2 in val1):
                        matches.append(f"{nfo_key} approx match: '{val1}' vs '{val2}'")
                        found = True
                        break

                    mismatches.append(f"{nfo_key}: NFO='{val1}' != MKV='{val2}' ({mkv_key})")
                    found = True # Found the key, but value mismatch
                    break
        
        if not found:
            missing.append(f"{nfo_key} (expected '{nfo_val}')")

    if matches:
        print(f"  ✅ Matches: {', '.join(matches)}")
        result['matches'] = len(matches)
    if mismatches:
        print(f"  ❌ Mismatches: {', '.join(mismatches)}")
        result['mismatches'] = len(mismatches)
        result['details'].extend(mismatches)
    if missing:
        print(f"  ⚠️ Missing in MKV: {', '.join(missing)}")
        result['missing'] = len(missing)
        result['details'].extend(missing)
    
    print("-" * 40)
    return result

def main():
    if not os.path.exists(MOVIES_DIR):
        print(f"Error: Directory {MOVIES_DIR} does not exist.")
        return

    all_folders = [os.path.join(MOVIES_DIR, d) for d in os.listdir(MOVIES_DIR) 
                   if os.path.isdir(os.path.join(MOVIES_DIR, d))]
    
    if not all_folders:
        print("No folders found.")
        return

    sample_folders = random.sample(all_folders, min(NUM_SAMPLES, len(all_folders)))
    
    print(f"Spot checking {len(sample_folders)} movies in {MOVIES_DIR}...\n")
    
    results = []
    for folder in sample_folders:
        results.append(check_movie(folder))

    # Summary
    print("\n" + "="*60)
    print(f"{'SUMMARY REPORT':^60}")
    print("="*60)
    print(f"{'Total Checked':<20}: {len(results)}")
    
    perfect = sum(1 for r in results if r['mismatches'] == 0 and r['missing'] == 0 and r['status'] == 'OK')
    with_issues = sum(1 for r in results if r['mismatches'] > 0 or r['missing'] > 0)
    errors = sum(1 for r in results if r['status'] != 'OK')
    
    print(f"{'Perfect Matches':<20}: {perfect}")
    print(f"{'With Issues':<20}: {with_issues}")
    print(f"{'Errors (No NFO/MKV)':<20}: {errors}")
    print("-" * 60)
    
    if with_issues > 0:
        print("\nTop Issues:")
        for r in results:
            if r['mismatches'] > 0 or r['missing'] > 0:
                print(f"  • {r['folder']}")
                for d in r['details']:
                    print(f"      - {d}")


if __name__ == "__main__":
    main()
