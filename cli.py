"""Command-line launcher that needs no installation.

    python cli.py learn
    python cli.py run
    python cli.py run --live
    python cli.py status
    python cli.py watch

The package lives under src/, which is not on sys.path in a fresh shell, so
`python -m email_distributor` only works after an install or with PYTHONPATH
set. This launcher puts src/ on the path first, so the tool runs straight from
a copied folder - the same reason run.pyw exists for the GUI, and the point of
the whole no-install design on a managed laptop.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from email_distributor.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
