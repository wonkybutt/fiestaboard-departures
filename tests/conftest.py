"""Test fixtures for the departures plugin."""

import json
from pathlib import Path

import pytest

from src.plugins.testing import create_mock_response


@pytest.fixture(autouse=True)
def reset_plugin_singletons():
    """Reset plugin singletons before each test."""
    yield


@pytest.fixture
def mock_api_response():
    """Fixture to create mock HTTP responses."""
    return create_mock_response


@pytest.fixture
def sample_manifest():
    """Load the plugin manifest for testing."""
    manifest_path = Path(__file__).parent.parent / "manifest.json"
    with open(manifest_path) as f:
        return json.load(f)


@pytest.fixture
def sample_config():
    """Sample configuration for testing."""
    # 2026-05-09: removed max_events — all events are tracked without a cap
    return {
        "enabled": True,
        "calendar_url": "https://example.com/calendar.ics",
        "timezone": "UTC",
    }
