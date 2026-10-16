import re
from pathlib import Path

from fastapi.testclient import TestClient

from botfoundry import __version__
from botfoundry.api.app import create_app
from botfoundry.config import Settings, TOKEN_HEADER

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
TOKEN = "test-token-1234567890"


def test_health_reports_package_version():
    client = TestClient(create_app(Settings(token=TOKEN)))
    body = client.get("/health", headers={TOKEN_HEADER: TOKEN}).json()
    assert body["engine_version"] == __version__


def test_changelog_top_entry_matches_version():
    match = re.search(r"^## \[(\d+\.\d+\.\d+)\]", CHANGELOG.read_text(encoding="utf-8"), re.M)
    assert match, "CHANGELOG.md has no release heading like '## [0.2.0] - date'"
    assert match.group(1) == __version__, (
        f"CHANGELOG top entry is {match.group(1)} but __version__ is {__version__}. "
        "Add a changelog entry or run scripts/bump.py."
    )
