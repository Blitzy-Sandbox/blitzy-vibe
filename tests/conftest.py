from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest
import tomli_w

from vibe.core.paths import global_paths
from vibe.core.paths.config_paths import unlock_config_paths


def pytest_configure(config: pytest.Config) -> None:
    """Register custom pytest markers programmatically.

    Registers the ``integration`` marker used by Gate 8 integration tests in
    ``tests/backend/test_blitzy_backend.py`` and
    ``tests/backend/test_anthropic_backend_extension.py``. Registration here
    (rather than in ``pyproject.toml`` ``[tool.pytest.ini_options].markers``)
    keeps ``pyproject.toml`` untouched at this checkpoint while still
    suppressing the ``PytestUnknownMarkWarning`` that would otherwise emit at
    test collection. This is the pytest-documented alternative for marker
    registration and is functionally equivalent to declaring the marker in
    the INI/TOML config (see
    https://docs.pytest.org/en/stable/how-to/mark.html).

    The ``integration`` marker is used to opt-in to live-network tests that
    hit real provider APIs (Blitzy ``/v1/api/chat``, Anthropic ``messages``).
    Such tests are SKIPPED by default at collection time when the relevant
    ``*_API_KEY`` environment variable is absent or set to the placeholder
    value ``"mock"``, and are intended to be invoked deliberately via
    ``uv run pytest -m integration`` when an operator wants to verify
    wire-protocol compatibility against a production endpoint.
    """
    config.addinivalue_line(
        "markers",
        "integration: marks tests that hit real provider APIs and require a "
        "live API key (skipped by default; opt-in via `pytest -m integration`)",
    )


def get_base_config() -> dict[str, Any]:
    return {
        "active_model": "devstral-latest",
        "providers": [
            {
                "name": "mistral",
                "api_base": "https://api.mistral.ai/v1",
                "api_key_env_var": "MISTRAL_API_KEY",
                "backend": "mistral",
            }
        ],
        "models": [
            {
                "name": "mistral-vibe-cli-latest",
                "provider": "mistral",
                "alias": "devstral-latest",
            }
        ],
        "enable_auto_update": False,
    }


@pytest.fixture(autouse=True)
def tmp_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    tmp_working_directory = tmp_path_factory.mktemp("test_cwd")
    monkeypatch.chdir(tmp_working_directory)
    return tmp_working_directory


@pytest.fixture(autouse=True)
def config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    tmp_path = tmp_path_factory.mktemp("vibe")
    config_dir = tmp_path / ".vibe"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.toml"
    config_file.write_text(tomli_w.dumps(get_base_config()), encoding="utf-8")

    monkeypatch.setattr(global_paths, "_DEFAULT_VIBE_HOME", config_dir)
    return config_dir


@pytest.fixture(autouse=True)
def _unlock_config_paths():
    unlock_config_paths()


@pytest.fixture(autouse=True)
def _mock_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "mock")


@pytest.fixture(autouse=True)
def _mock_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock platform to be Linux with /bin/sh shell for consistent test behavior.

    This ensures that platform-specific system prompt generation is consistent
    across all tests regardless of the actual platform running the tests.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("SHELL", "/bin/sh")


@pytest.fixture(autouse=True)
def _mock_update_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vibe.cli.update_notifier.update.UPDATE_COMMANDS", ["true"])
