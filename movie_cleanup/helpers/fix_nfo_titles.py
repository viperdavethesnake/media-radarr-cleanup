#!/usr/bin/env python3

import os
import re
import argparse

from dotenv import load_dotenv

load_dotenv()

MOVIES_DIR = os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'movies')

def clean_title(title_text):
    # Replace underscores with spaces
    cleaned = title_text.replace('_', ' ')
    
    # Remove year suffix like " (2025)" or "(2025)" at the end
    # The NFOs seem to have "Title_(Year)", so after replacing _ with space, it is "Title (Year)"
    cleaned = re.sub(r'\s*\(\d{4}\)$', '', cleaned)
    
    return cleaned.strip()

def sanitize_for_comparison(text):
    # Remove non-alphanumeric and lowercase
    return re.sub(r'[^a-z0-9]', '', text.lower())

def process_nfo(nfo_path, dry_run=True):
    try:
        with open(nfo_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Find current title
        title_match = re.search(r'<title>(.*?)</title>', content, re.IGNORECASE)
        if not title_match:
            return None # No title tag found
            
        current_title = title_match.group(1)
        
        # Check if it needs fixing (has underscores)
        if '_' not in current_title:
            return None # Already clean?
            
        # Calculate candidate cleaned title
        candidate_title = clean_title(current_title)
        
        # Find originaltitle for comparison
        orig_match = re.search(r'<originaltitle>(.*?)</originaltitle>', content, re.IGNORECASE)
        original_title = orig_match.group(1) if orig_match else None
        
        final_title = candidate_title
        
        # If original title exists and matches the cleaned title (ignoring punctuation/case),
        # prefer original title as it likely has better punctuation (e.g. "Mission: Impossible" vs "Mission Impossible")
        if original_title:
            if sanitize_for_comparison(original_title) == sanitize_for_comparison(candidate_title):
                final_title = original_title
        
        if final_title == current_title:
            return None
            
        # Perform replacement
        new_content = content.replace(f'<title>{current_title}</title>', f'<title>{final_title}</title>')
        
        if not dry_run:
            with open(nfo_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
                
        return (current_title, final_title)
        
    except Exception as e:
        print(f"Error processing {nfo_path}: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="Fix movie.nfo titles")
    parser.add_argument('--apply', action='store_true', help="Apply changes (disable dry-run)")
    args = parser.parse_args()
    
    dry_run = not args.apply
    
    print(f"Scanning {MOVIES_DIR}...")
    print(f"Mode: {'DRY RUN (No changes)' if dry_run else 'APPLYING CHANGES'}")
    print("-" * 60)
    
    count = 0
    changed = 0
    
    for root, dirs, files in os.walk(MOVIES_DIR):
        if 'movie.nfo' in files:
            nfo_path = os.path.join(root, 'movie.nfo')
            count += 1
            
            result = process_nfo(nfo_path, dry_run=dry_run)
            if result:
                old, new = result
                print(f"Fixing: {os.path.basename(root)}")
                print(f"  Old: {old}")
                print(f"  New: {new}")
                changed += 1
                
    print("-" * 60)
    print(f"Scanned {count} NFO files.")
    print(f"Proposed changes: {changed}" if dry_run else f"Fixed {changed} files.")

if __name__ == "__main__":
    main()
