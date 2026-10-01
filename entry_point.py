#!/usr/bin/env python3
"""neuralOS standalone binary entry point — used by PyInstaller."""
import sys
import os

# Ensure the package directory is on the path (PyInstaller bundles it)
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

from neuralosd.cli import main

if __name__ == "__main__":
    main()
