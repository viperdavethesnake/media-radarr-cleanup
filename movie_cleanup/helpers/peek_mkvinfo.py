#!/usr/bin/env python3
import os
import random
import subprocess
from dotenv import load_dotenv

load_dotenv()

SERVARR_DIR = os.getenv('RC_SERVARR_DIR') or os.path.join(os.getenv('RC_MEDIA_BASE', '/storage/media'), 'servarr')
TAGGED_DIR = os.path.join(SERVARR_DIR, 'tagged')
N = 5  # Number of random folders to pick

def peek_mkvinfo(movie_dir):
    mkvs = [f for f in os.listdir(movie_dir) if f.lower().endswith('.mkv')]
    if not mkvs:
        print(f"== {movie_dir} ==\n  (No MKV found)\n")
        return
    mkv_file = os.path.join(movie_dir, mkvs[0])
    print(f"== {mkv_file} ==")
    try:
        output = subprocess.run(['mkvinfo', mkv_file], capture_output=True, text=True)
        lines = output.stdout.splitlines()[:30]
        for line in lines:
            print(line)
    except Exception as e:
        print(f"  Error running mkvinfo: {e}")
    print("\n" + "="*40 + "\n")

def main():
    folders = [os.path.join(TAGGED_DIR, d) for d in os.listdir(TAGGED_DIR)
               if os.path.isdir(os.path.join(TAGGED_DIR, d))]
    random.shuffle(folders)
    for folder in folders[:N]:
        peek_mkvinfo(folder)

if __name__ == "__main__":
    main()

