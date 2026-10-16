import pytest
from fastapi.testclient import TestClient

from botfoundry.api.app import create_app
from botfoundry.config import Settings, TOKEN_HEADER

TOKEN = "test-token-1234567890"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(Settings(token=TOKEN)))


def test_health_with_valid_token(client):
    r = client.get("/health", headers={TOKEN_HEADER: TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["ai_available"] is False


def test_health_without_token_is_rejected(client):
    assert client.get("/health").status_code == 401


def test_health_with_wrong_token_is_rejected(client):
    assert client.get("/health", headers={TOKEN_HEADER: "nope"}).status_code == 401


def test_cors_allows_tauri_origin_preflight(client):
    r = client.options(
        "/health",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": TOKEN_HEADER,
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://tauri.localhost"


def test_cors_rejects_unknown_origin(client):
    r = client.options(
        "/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in r.headers


def test_settings_require_strong_token(monkeypatch):
    monkeypatch.setenv("BOTFOUNDRY_TOKEN", "short")
    with pytest.raises(RuntimeError):
        Settings.from_env()
