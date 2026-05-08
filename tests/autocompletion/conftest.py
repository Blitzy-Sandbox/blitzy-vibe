from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import PropertyMock, patch

import pytest


@pytest.fixture(autouse=True)
def _no_bundled_skills(tmp_path: Path) -> Iterator[None]:
    """Prevent bundled skills from leaking into autocompletion tests."""
    fake = tmp_path / "_no_bundled"
    with patch(
        "vibe.core.skills.manager.BUNDLED_SKILLS_DIR",
    ) as mock_dir:
        type(mock_dir).path = PropertyMock(return_value=fake)
        yield
