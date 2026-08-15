"""Test runner that needs no installation.

    python run_tests.py            # run everything
    python run_tests.py -v         # verbose
    python run_tests.py test_rules # one module

Same reason as cli.py and run.pyw: the package lives under src/, which a fresh
shell does not have on sys.path, so plain `python -m unittest discover` cannot
import it.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbosity = 2 if "-v" in sys.argv or "--verbose" in sys.argv else 1

    loader = unittest.TestLoader()
    if args:
        suite = loader.loadTestsFromNames(args)
    else:
        suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))

    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
