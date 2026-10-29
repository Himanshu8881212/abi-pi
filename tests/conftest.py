import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]


def pytest_collection_modifyitems(config, items):
    """Live tests drive GPT-6.1 Sol through Pi (minutes, tokens): opt in with ABI_PI_LIVE=1."""
    if os.environ.get("ABI_PI_LIVE") != "1":
        skip = pytest.mark.skip(reason="live LLM test: set ABI_PI_LIVE=1")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip)
