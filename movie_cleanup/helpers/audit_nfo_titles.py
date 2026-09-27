#!/usr/bin/env python3

import os
import re
import argparse

from dotenv import load_dotenv

load_dotenv()

MOVIES_DIR = os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'movies')

def is_clean_title(title_text):
    # Check for underscores
    if '_' in title_text:
        return False, "Contains underscores"
    
    # Check for year suffix like " (2025)" or "(2025)" at the end
    if re.search(r'\(\d{4}\)$', title_text):
        return False, "Contains year suffix"
        
    return True, "Clean"

def process_nfo(nfo_path):
    try:
        with open(nfo_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Find current title
        title_match = re.search(r'<title>(.*?)</title>', content, re.IGNORECASE)
        if not title_match:
            return "MISSING_TAG", None
            
        current_title = title_match.group(1)
        
        is_clean, reason = is_clean_title(current_title)
        
        if not is_clean:
            return "UNCLEAN", f"{current_title} [{reason}]"
            
        return "CLEAN", current_title
        
    except Exception as e:
        return "ERROR", str(e)

def main():
    print(f"Auditing NFO titles in {MOVIES_DIR}...")
    print("-" * 60)
    
    stats = {
        "CLEAN": 0,
        "UNCLEAN": 0,
        "MISSING_TAG": 0,
        "ERROR": 0
    }
    
    unclean_files = []
    
    for root, dirs, files in os.walk(MOVIES_DIR):
        if 'movie.nfo' in files:
            nfo_path = os.path.join(root, 'movie.nfo')
            
            status, detail = process_nfo(nfo_path)
            stats[status] += 1
            
            if status == "UNCLEAN":
                unclean_files.append((os.path.basename(root), detail))
                # print(f"UNCLEAN: {os.path.basename(root)} -> {detail}")
            elif status == "ERROR":
                print(f"ERROR: {os.path.basename(root)} -> {detail}")

    print("-" * 60)
    if unclean_files:
        print(f"Found {len(unclean_files)} unclean titles:")
        for folder, detail in unclean_files:
            print(f"  {folder}: {detail}")
        print("-" * 60)
    
    print("Audit Summary:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print(f"  TOTAL: {sum(stats.values())}")

if __name__ == "__main__":
    main()
