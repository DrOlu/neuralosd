#!/usr/bin/env python3
"""neuralOS standalone binary entry point — used by Nuitka / PyInstaller."""
import os
import sys

# Frozen binaries frequently come up with an ascii or cp1252 default encoding
# (no locale). Force UTF-8 on stdio so em-dashes, box-drawing characters and
# other non-ASCII output never raise UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# Make sibling modules importable when running from source.
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

from neuralosd.cli import main  # noqa: E402

if __name__ == "__main__":
    main()