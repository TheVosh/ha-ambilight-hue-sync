"""Shared test fixtures."""

import sys
from pathlib import Path

# Add lib/ to path so dtls_psk can be imported without installing
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
