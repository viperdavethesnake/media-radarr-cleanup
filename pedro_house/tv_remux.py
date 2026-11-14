#!/usr/bin/env python3
"""Pedro house (site 33) tv remux: the shared tv_mkv_remux_cleanroom with playback
compatibility on (AC3 audio, DV7 base layer only, DV5 to review).
See pedro_house/README.md."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tv_cleanup import tv_mkv_remux_cleanroom as remux

if __name__ == '__main__':
    remux.main(compat=True)
