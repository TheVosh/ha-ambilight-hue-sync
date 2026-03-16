"""Shared test fixtures."""

import sys
from pathlib import Path

# Add custom_components/ to path so hue_entertainment.dtls_psk can be imported
sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))
