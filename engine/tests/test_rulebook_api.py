import json

import pytest
from fastapi.testclient import TestClient

from botfoundry.api.app import create_app
from botfoundry.config import Settings, TOKEN_HEADER
from botfoundry.rules.constraints import SystemConstraints
from botfoundry.rules.paths import WINDOWS
from botfoundry.storage import Storage

TOKEN = "test-token-1234567890"
H = {TOKEN_HEADER: TOKEN}
DL = "C:\\Users\\Me\\Downloads"
CONSTRAINTS = SystemConstraints(protected_paths=("C:\\Windows",), too_broad_paths=("C:\\Users\\Me",), style=WINDOWS)


def doc(**changes):
    data = {"bot_id": "fixer", "name": "Fixer", "allowed_folders": [DL], "forbidden_folders": [DL + "\\Keep"],
            "permissions": {"fs.read": "allow", "fs.move": "ask"}, "limits": {"max_files_per_run": 100}}
    data.update(changes)
    return data


@pytest.fixture()
def client(tmp_path):
    storage = Storage.open(tmp_path / "rb.db")
    app = create_app(Settings(token=TOKEN), storage, constraints=CONSTRAINTS)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    storage.close()


ENDPOINTS = [("GET", "/permissions", None), ("POST", "/rulebooks/validate", {}), ("GET", "/rulebooks", None),
             ("GET", "/rulebooks/x", None), ("GET", "/rulebooks/x/versions", None),
             ("GET", "/rulebooks/x/versions/1", None), ("PUT", "/rulebooks/x", {}),
             ("POST", "/rulebooks/x/evaluate", {"permission": "fs.read"})]


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_rulebook_endpoints_require_the_token(client, method, path, body):
    r = client.request(method, path, json=body)
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_permission_vocabulary_endpoint(client):
    perms = {p["name"]: p for p in client.get("/permissions", headers=H).json()["permissions"]}
    assert perms["fs.delete"]["system_ceiling"] == "deny" and perms["process.run_tests"]["system_ceiling"] == "ask"
    assert perms["fs.read"]["system_ceiling"] == "allow" and perms["fs.move"]["risk"] == "medium"


def test_validate_endpoint(client):
    good = client.post("/rulebooks/validate", json=doc(), headers=H).json()
    assert good["ok"] is True and good["issues"] == []
    bad = client.post("/rulebooks/validate", json=doc(permissions={"fs.delete": "allow", "fs.mvoe": "allow"}), headers=H).json()
    assert bad["ok"] is False
    assert {"rulebook.exceeds_system_limit", "rulebook.unknown_permission"} <= {i["code"] for i in bad["issues"]}
    assert client.post("/rulebooks/validate", json={"nope": 1}, headers=H).json()["issues"][0]["code"] == "schema.invalid"


def test_saving_creates_versions_only_when_something_changed(client):
    first = client.put("/rulebooks/fixer?note=initial", json=doc(), headers=H)
    assert first.status_code == 200 and first.json()["version"] == 1 and first.json()["created"] is True
    again = client.put("/rulebooks/fixer", json=doc(), headers=H).json()
    assert again["version"] == 1 and again["created"] is False
    changed = client.put("/rulebooks/fixer?note=tighter", json=doc(permissions={"fs.read": "allow"}), headers=H).json()
    assert changed["version"] == 2 and changed["created"] is True
    latest = client.get("/rulebooks/fixer", headers=H).json()
    assert latest["version"] == 2 and latest["note"] == "tighter" and latest["rulebook"]["permissions"] == {"fs.read": "allow"}
    versions = client.get("/rulebooks/fixer/versions", headers=H).json()["versions"]
    assert [v["version"] for v in versions] == [2, 1] and versions[1]["note"] == "initial"
    assert client.get("/rulebooks/fixer/versions/1", headers=H).json()["rulebook"]["permissions"]["fs.move"] == "ask"
    assert [r["version"] for r in client.get("/rulebooks", headers=H).json()["rulebooks"]] == [2]


def test_a_rulebook_with_errors_is_never_saved(client):
    r = client.put("/rulebooks/fixer", json=doc(allowed_folders=["C:\\Windows\\Temp"]), headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "rulebook_invalid"
    assert "rulebook.folder_protected" in {i["code"] for i in r.json()["error"]["details"]["issues"]}
    assert client.get("/rulebooks", headers=H).json() == {"rulebooks": []}
    assert client.put("/rulebooks/fixer", json={"bot_id": "fixer"}, headers=H).status_code == 422


def test_warnings_do_not_prevent_saving_but_are_returned(client):
    r = client.put("/rulebooks/fixer", json=doc(default_policy="allow"), headers=H)
    assert r.status_code == 200
    assert "rulebook.permissive_default" in {i["code"] for i in r.json()["validation"]["issues"]}


def test_bot_id_must_match_the_url(client):
    r = client.put("/rulebooks/other", json=doc(), headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "id_mismatch"


def test_missing_rulebooks_are_404(client):
    for path in ("/rulebooks/ghost", "/rulebooks/ghost/versions", "/rulebooks/ghost/versions/1"):
        r = client.get(path, headers=H)
        assert r.status_code == 404 and r.json()["error"]["code"] == "rulebook_not_found"
    client.put("/rulebooks/fixer", json=doc(), headers=H)
    assert client.get("/rulebooks/fixer/versions/7", headers=H).status_code == 404


def test_versions_cannot_be_deleted_or_edited(client):
    client.put("/rulebooks/fixer", json=doc(), headers=H)
    assert client.delete("/rulebooks/fixer", headers=H).status_code == 405
    assert client.put("/rulebooks/fixer/versions/1", json=doc(), headers=H).status_code == 405


def test_evaluate_gives_the_decision_the_engine_would_make(client):
    client.put("/rulebooks/fixer", json=doc(), headers=H)

    def ask(permission, paths=()):
        r = client.post("/rulebooks/fixer/evaluate", json={"permission": permission, "paths": list(paths)}, headers=H)
        assert r.status_code == 200, r.text
        return r.json()

    allow = ask("fs.read", [DL + "\\a.txt"])
    assert allow["version"] == 1 and allow["decision"]["outcome"] == "allow" and allow["decision"]["code"] == "ok"
    assert ask("fs.move", [DL + "\\a.txt"])["decision"]["outcome"] == "ask"
    assert ask("fs.delete")["decision"]["code"] == "system.limit"
    assert ask("fs.write")["decision"]["code"] == "rulebook.not_granted"
    outside = ask("fs.read", [DL + "\\ok.txt", "C:\\Users\\Me\\Documents\\x"])["decision"]
    assert outside["outcome"] == "deny" and outside["code"] == "path.outside_allowed"
    assert [p["allowed"] for p in outside["paths"]] == [True, False]
    assert ask("fs.read", [DL + "\\Keep\\private.txt"])["decision"]["code"] == "path.forbidden"
    assert ask("fs.teleport")["decision"]["code"] == "permission.unknown"


def test_evaluate_uses_the_latest_version(client):
    client.put("/rulebooks/fixer", json=doc(), headers=H)
    client.put("/rulebooks/fixer", json=doc(permissions={"fs.read": "deny"}), headers=H)
    r = client.post("/rulebooks/fixer/evaluate", json={"permission": "fs.read", "paths": [DL + "\\a"]}, headers=H).json()
    assert r["version"] == 2 and r["decision"]["code"] == "rulebook.denied"


def test_evaluate_edge_cases(client):
    assert client.post("/rulebooks/ghost/evaluate", json={"permission": "fs.read"}, headers=H).status_code == 404
    client.put("/rulebooks/fixer", json=doc(), headers=H)
    too_many = {"permission": "fs.read", "paths": [DL] * 1001}
    assert client.post("/rulebooks/fixer/evaluate", json=too_many, headers=H).status_code == 422
    assert client.post("/rulebooks/fixer/evaluate", json={}, headers=H).status_code == 422


def test_workflows_can_name_the_bot_that_governs_them(client):
    wf = {"id": "w", "name": "W", "bot_id": "fixer", "nodes": [{"id": "start", "type": "core.manual_trigger"}], "edges": []}
    assert client.put("/workflows/w", json=wf, headers=H).status_code == 200
    assert client.get("/workflows/w", headers=H).json()["bot_id"] == "fixer"
    old_style = {k: v for k, v in wf.items() if k != "bot_id"}
    assert client.put("/workflows/w2", json={**old_style, "id": "w2"}, headers=H).status_code == 200
    assert client.get("/workflows/w2", headers=H).json()["bot_id"] is None
    assert client.put("/workflows/w3", json={**wf, "id": "w3", "bot_id": "bad id!"}, headers=H).status_code == 422
