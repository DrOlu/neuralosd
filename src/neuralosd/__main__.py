"""`python -m neuralosd` — same entry point as the `neuralosd` console script.

The console script (pyproject [project.scripts]) is the documented install
path, but `python -m neuralosd` is the standard fallback CI, subprocess smoke
tests and venv-less checkouts reach for. Without this module it dies with
"No module named neuralosd.__main__; 'neuralosd' is a package and cannot be
directly executed" — which is also why the serve subprocess regression test
below (tests/test_multi_instance.py) needs this file to exist.
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
