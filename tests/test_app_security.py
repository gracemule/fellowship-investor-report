"""The web app's gate: nothing is reachable without the shared password, writes must come from the
app's own origin, bad uploads are refused before they touch the database, and production refuses to
start without a password."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chui_reporter.app.auth import Auth
from chui_reporter.app.main import create_app


class _Store:
    asked = 0

    def status_counts(self):
        _Store.asked += 1
        return {}


class _Runtime:
    store = _Store()

    def shutdown(self):
        pass


@pytest.fixture()
def client():
    app = create_app(runtime=_Runtime(), auth=Auth("correct horse", "test-secret"))
    with TestClient(app, base_url="http://app.test") as c:
        yield c


def test_the_shell_and_health_are_public_but_data_is_not(client):
    assert client.get("/").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}
    for path in ("/api/state", "/api/history", "/api/report", "/api/notes", "/api/events"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/run", json={}).status_code == 401
    assert client.get("/brand/Fonts/Larken/Larken Regular.ttf").status_code == 401


def test_the_wrong_password_is_refused_and_the_right_one_opens_the_session(client):
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    ok = client.post("/api/login", json={"password": "correct horse"})
    assert ok.status_code == 200
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/session").status_code == 200


def test_repeated_wrong_guesses_are_throttled_even_for_the_right_password(client):
    for _ in range(5):
        client.post("/api/login", json={"password": "wrong"})
    assert client.post("/api/login", json={"password": "correct horse"}).status_code == 429


def test_a_forged_or_expired_cookie_does_not_pass():
    a = Auth("pw", "secret")
    assert a.valid(a.issue())
    assert not a.valid("1700000000.deadbeef") and not a.valid("garbage") and not a.valid(None)
    stale = "1.%s" % a._sig("1")
    assert not a.valid(stale), "a correctly signed but long-expired token is refused"
    assert not Auth("pw", "other-secret").valid(a.issue()), "a token from another key is refused"


def test_writes_from_another_origin_are_refused(client):
    client.post("/api/login", json={"password": "correct horse"})
    r = client.post("/api/steer", json={"text": "hi"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_uploads_are_validated_before_anything_is_stored(client):
    client.post("/api/login", json={"password": "correct horse"})
    bad_hash = client.put("/api/sync/file?path=a.xlsx&sha256=nothex", content=b"x")
    assert bad_hash.status_code == 400
    for path in ("../etc/passwd", "ok/../../x", "C:/windows/x.xlsx"):
        r = client.post("/api/sync/plan", json={"manifest": [{"path": path, "sha256": "0" * 64, "size": 1}]})
        assert r.status_code == 400, path
    r = client.post("/api/sync/plan", json={"manifest": [{"path": "a.xlsx", "sha256": "xyz", "size": 1}]})
    assert r.status_code == 400


def test_a_leading_slash_is_normalised_into_the_workspace_not_honoured():
    from chui_reporter.workspace.sync import safe_path

    assert safe_path("/abs/path.xlsx") == "abs/path.xlsx"
    assert safe_path("a\\b\\c.xlsx") == "a/b/c.xlsx"


def test_static_files_always_revalidate_so_a_deploy_shows_up_at_once(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_production_refuses_to_start_without_a_password(monkeypatch):
    monkeypatch.delenv("CHUI_ACCESS_PASSWORD", raising=False)
    monkeypatch.delenv("CHUI_ENV", raising=False)
    with pytest.raises(RuntimeError, match="CHUI_ACCESS_PASSWORD"):
        Auth.from_env()
    monkeypatch.setenv("CHUI_ENV", "dev")
    assert Auth.from_env().disabled


def test_dev_helpers_are_not_mounted_in_production(client):
    assert client.get("/api/dev/tree").status_code in (401, 404)


def test_uptime_monitors_can_ping_with_head_and_the_ping_never_wakes_the_database(client):
    """Free uptime monitors send HEAD. A ping every few minutes must not touch a scale-to-zero database."""
    before = _Store.asked
    for _ in range(3):
        assert client.head("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 200
    assert _Store.asked == before, "liveness pings do not query the database"
    assert client.head("/readyz").status_code == 200 and client.get("/readyz").json()["database"] is True
    assert _Store.asked > before, "readiness does check the database"
    assert client.head("/api/state").status_code in (401, 405), "only the health routes are public"


def test_the_startup_report_names_settings_and_never_shows_values():
    from chui_reporter.app.settings_check import report

    line = report({"DATABASE_URL": "postgresql://user:hunter2@host/db", "CHUI_ACCESS_PASSWORD": "  ", "DEEPSEEK_API_KEY": "sk-secret",
                   "CHUI_ACCES_PASSWORD": "typo", "CHUI_CONVERTER_TOKEN": "tok"})
    assert "DATABASE_URL=set(" in line and "CHUI_ACCESS_PASSWORD=EMPTY" in line and "TAVILY_API_KEY=MISSING" in line
    assert "CHUI_ACCES_PASSWORD" in line.split("misspelled:")[1], "a near-miss name is pointed out"
    for secret in ("hunter2", "sk-secret", "typo", "postgresql://"):
        assert secret not in line


def test_production_refuses_to_start_without_a_password_and_says_what_it_can_see(monkeypatch):
    monkeypatch.delenv("CHUI_ENV", raising=False)
    monkeypatch.setenv("CHUI_ACCESS_PASSWORD", "")
    with pytest.raises(RuntimeError, match="CHUI_ACCESS_PASSWORD=EMPTY"):
        Auth.from_env()
