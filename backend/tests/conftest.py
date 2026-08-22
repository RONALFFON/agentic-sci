from __future__ import annotations

import os
import sys
from pathlib import Path


# The host environment may expose DEBUG=release, while Settings expects a bool.
if str(os.environ.get("DEBUG", "")).strip().lower() not in {
    "1", "0", "true", "false", "yes", "no", "on", "off"
}:
    os.environ["DEBUG"] = "true"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
