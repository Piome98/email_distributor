"""Double-click launcher for the desktop UI.

The .pyw extension makes Windows run this with pythonw.exe, so no console
window appears behind the app. Keeping the launcher at the project root means
the tool can be run straight from a copied folder, with no install step and no
changes to PATH - which is what makes it workable on a managed laptop.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from email_distributor.ui.app import main  # noqa: E402

if __name__ == "__main__":
    main()
