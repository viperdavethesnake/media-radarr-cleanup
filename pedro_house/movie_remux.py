#!/usr/bin/env python3
"""Pedro house (site 33) movie remux: the shared mkv_remux_cleanroom with playback
compatibility on (AC3 audio, DV7 base layer only, DV5 converted to HDR10).
Run this at site 33 only, never at site 36. See pedro_house/README.md."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from movie_cleanup import mkv_remux_cleanroom as remux

if __name__ == '__main__':
    remux.main(compat=True)
