"""Make src/ and tests/ importable, and reset the module-level AOI per test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


@pytest.fixture(autouse=True)
def lhende_aoi():
    """
    Every test runs against the Lhende box.

    gunw_reader keeps the active AOI in a module global that set_aoi() rewrites,
    so without this a test that switched AOI would leak into the next one and
    the failure would appear somewhere unrelated.
    """
    import gunw_reader
    gunw_reader.set_aoi("lhende")
    yield
    gunw_reader.set_aoi("langtang")
