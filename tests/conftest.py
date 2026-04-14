"""
Pytest bootstrap: point the app at tests/fixtures/minimal_config.yaml before ``import main``.

Run from the ``migration-engine`` directory::

    pip install -r requirements-dev.txt
    pytest
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
os.environ["MIGRATION_ENGINE_CONFIG"] = str(_ROOT / "tests" / "fixtures" / "minimal_config.yaml")


@pytest.fixture
def main_module():
    import main as m

    return m


@pytest.fixture
def ui_module():
    import ui as u

    return u
