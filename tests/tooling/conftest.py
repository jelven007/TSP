"""Keep CLI helper imports separate from the backend's ``scripts`` package."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for directory in (ROOT / "scripts", ROOT / "scripts/research", ROOT / "backend"):
    sys.path.insert(0, str(directory))
