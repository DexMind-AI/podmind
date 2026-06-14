"""pytest configuration for podmind tests.

Tests run in isolation — they don't read the user's actual vault. Set
PODMIND_DATA_ROOT to a tmp dir before any podmind import to keep paths
predictable.
"""
import os
import sys
import tempfile
from pathlib import Path

# Set isolation env BEFORE podmind imports happen.
_TMP = Path(tempfile.mkdtemp(prefix="podmind-test-"))
(_TMP / "raw" / "episodes").mkdir(parents=True, exist_ok=True)
(_TMP / "wiki" / "topics").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PODMIND_DATA_ROOT", str(_TMP))

# Ensure the package is importable when tests run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
