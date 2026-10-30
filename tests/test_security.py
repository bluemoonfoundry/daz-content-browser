"""Security regression tests: token auth, Host/Origin guard, CORS, content-root scoping,
and credential redaction. Run against both the demo app and a production-like fixture
(APP_MODE=production with fake managers)."""
import json
import types

import pytest
from fastapi.testclient import TestClient

from api_harness import FakeDesktop, FakeScriptServer, ServerHarness, demo_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "http://127.0.0.1"

# (method, path, json body) for every privileged endpoint
PRIVILEGED = [
    ("POST", "/api/v1/update", {"force": False}),
    ("DELETE", "/api/v1/update", None),
    ("POST", "/api/v1/products/SKU1/open", None),
    ("GET", "/api/v1/browseproduct/SKU1", None),
    ("GET", "/api/v1/settings", None),
    ("PUT", "/api/v1/settings", {"cms_host": "evil"}),
    ("POST", "/api/v1/settings/test-db", {"cms_host": "127.0.0.1"}),
    ("POST", "/api/v1/scene/load", {"path": "C:/x.duf"}),
    ("POST", "/api/v1/files/reveal", {"path": "C:/x.duf"}),
    ("GET", "/api/v1/assets/thumbnail?path=C:/x.duf", None),
]


@pytest.fixture
def app(request, tmp_path):
    """The demo app, or the app of the ``mode`` a test asks for."""
    if "mode" in request.fixturenames:
        return request.getfixturevalue("mode").app
    return demo_app(tmp_path)


@pytest.fixture
def anon(app):
    return TestClient(app, base_url=BASE)


@pytest.fixture
def authed(app):
    return TestClient(app, base_url=BASE, headers=AUTH)


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"cms_user": "dbuser", "cms_password": "s3cret"}), encoding="utf-8")
    monkeypatch.setenv("VAB_SETTINGS_PATH", str(path))
    return path


class _FakeScriptServer(FakeScriptServer):
    def __init__(self):
        super().__init__(available=True)


@pytest.fixture
def content_root(tmp_path):
    root = tmp_path / "library"
    (root / "People").mkdir(parents=True)
    asset = root / "People" / "Ethan.duf"
    asset.write_text("{}", encoding="utf-8")
    (root / "People" / "Ethan.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "passwords.duf").write_text("{}", encoding="utf-8")
    (outside / "passwords.png").write_bytes(b"\x89PNG\r\n\x1a\nsecret")
    return root, asset, outside


@pytest.fixture(params=["demo", "production"])
def mode(request, content_root, monkeypatch, tmp_path):
    """Runs a test in demo mode and in a production-like mode with fake adapters."""
    root, _, _ = content_root
    fake = _FakeScriptServer()
    desktop = FakeDesktop()
    if request.param == "production":
        from catalog.active_index import ActiveIndex
        from catalog.generations import GenerationStore
        from catalog.store import CatalogStore
        from managers.job_store import JobStore

        db = str(tmp_path / "catalog.db")
        gens = GenerationStore(db)
        harness = ServerHarness(CatalogStore(db), gens, ActiveIndex(gens, str(tmp_path / "chroma")),
                                JobStore(str(tmp_path / "jobs.db")), script_server=fake,
                                desktop=desktop)
        app = harness.app
    else:
        app = demo_app(tmp_path)
    monkeypatch.setattr(app.state.services.content, "content_roots", lambda: [str(root)])
    return types.SimpleNamespace(name=request.param, fake=fake, desktop=desktop, app=app)


# ── Authentication ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,body", PRIVILEGED)
def test_privileged_endpoints_reject_missing_token(anon, mode, settings_file, method, path, body):
    r = anon.request(method, path, json=body)
    assert r.status_code == 401, (path, r.text)
    assert mode.fake.loaded == []
    assert mode.desktop.launched == [] and mode.desktop.opened == []


@pytest.mark.parametrize("method,path,body", PRIVILEGED)
def test_privileged_endpoints_reject_wrong_token(app, mode, settings_file, method, path, body):
    c = TestClient(app, base_url=BASE, headers={"Authorization": "Bearer nope"})
    assert c.request(method, path, json=body).status_code == 401


def test_settings_write_is_not_persisted_without_token(anon, settings_file):
    anon.put("/api/v1/settings", json={"cms_host": "attacker.example"})
    assert "attacker.example" not in settings_file.read_text(encoding="utf-8")


def test_read_endpoints_open_by_default(anon):
    for path in ("/api/v1/status", "/api/v1/products", "/api/v1/filters", "/api/v1/update/status"):
        assert anon.get(path).status_code == 200, path
    assert anon.post("/api/v1/search", json={"query": "dress"}).status_code == 200


def test_read_endpoints_can_require_token(anon, authed, monkeypatch):
    monkeypatch.setenv("VAB_REQUIRE_TOKEN_FOR_READS", "1")
    assert anon.get("/api/v1/products").status_code == 401
    assert anon.post("/api/v1/query", json={"prompt": "dress"}).status_code == 401
    assert authed.get("/api/v1/products").status_code == 200


# ── Browser session cookie ─────────────────────────────────────────────────────

def test_session_cookie_authenticates_ui(app, settings_file):
    c = TestClient(app, base_url=BASE)
    r = c.post("/api/v1/auth/session", headers={"Origin": "http://127.0.0.1"})
    assert r.status_code == 200
    assert TOKEN not in r.text
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie
    assert c.get("/api/v1/settings").status_code == 200


def test_session_requires_browser_origin(anon):
    assert anon.post("/api/v1/auth/session").status_code == 403


def test_session_rejects_foreign_origin(anon):
    r = anon.post("/api/v1/auth/session", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "set-cookie" not in r.headers


# ── Host / Origin / CORS ───────────────────────────────────────────────────────

def test_dns_rebinding_host_rejected(app):
    c = TestClient(app, base_url="http://evil.example", headers=AUTH)
    assert c.get("/api/v1/status").status_code == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost:3000"])
def test_foreign_origin_rejected_even_with_token(authed, origin):
    r = authed.get("/api/v1/status", headers={"Origin": origin})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_configured_ui_origin(authed):
    r = authed.options(
        "/api/v1/search",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_same_origin_ui_accepted(authed):
    assert authed.get("/api/v1/status", headers={"Origin": "http://127.0.0.1"}).status_code == 200


# ── Content-root scoping ───────────────────────────────────────────────────────

def test_thumbnail_served_inside_root(authed, mode, content_root):
    _, asset, _ = content_root
    r = authed.get("/api/v1/assets/thumbnail", params={"path": str(asset)})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


@pytest.mark.parametrize("which", ["outside", "traversal", "relative"])
def test_thumbnail_outside_root_rejected(authed, mode, content_root, which):
    root, _, outside = content_root
    path = {
        "outside": str(outside / "passwords.duf"),
        "traversal": str(root / ".." / "secret" / "passwords.duf"),
        "relative": "secret/passwords.duf",
    }[which]
    r = authed.get("/api/v1/assets/thumbnail", params={"path": path})
    assert r.status_code == 403
    assert b"secret" not in r.content


def test_scene_load_scoped_to_roots(authed, mode, content_root):
    _, asset, outside = content_root
    assert authed.post("/api/v1/scene/load", json={"path": str(outside / "passwords.duf")}).status_code == 403
    assert mode.fake.loaded == []
    assert authed.post("/api/v1/scene/load", json={"path": str(asset)}).status_code == 200


def test_reveal_scoped_to_roots(authed, mode, content_root):
    if mode.name == "demo":
        pytest.skip("reveal launches the real file explorer outside the production fixture")
    _, asset, outside = content_root
    assert authed.post("/api/v1/files/reveal", json={"path": str(outside / "passwords.duf")}).status_code == 403
    assert mode.desktop.launched == []
    assert authed.post("/api/v1/files/reveal", json={"path": str(asset)}).status_code == 200
    assert len(mode.desktop.launched) == 1


def test_reveal_outside_root_rejected_in_demo(authed, mode, content_root):
    _, _, outside = content_root
    assert authed.post("/api/v1/files/reveal", json={"path": str(outside / "passwords.duf")}).status_code == 403


# ── Credential redaction ───────────────────────────────────────────────────────

def test_settings_never_return_password(authed, settings_file):
    r = authed.get("/api/v1/settings")
    assert r.status_code == 200
    assert "s3cret" not in r.text and "cms_password" not in r.json()
    assert r.json()["cms_password_set"] is True

    r = authed.put("/api/v1/settings", json={"cms_password": "n3w-s3cret"})
    assert r.status_code == 200
    assert "n3w-s3cret" not in r.text and "cms_password" not in r.json()
    assert json.loads(settings_file.read_text(encoding="utf-8"))["cms_password"] == "n3w-s3cret"

    # Blank password from the UI keeps the stored one
    authed.put("/api/v1/settings", json={"cms_password": "", "cms_host": "db.local"})
    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert stored["cms_password"] == "n3w-s3cret" and stored["cms_host"] == "db.local"


# ── Unit: resolve_within_roots ─────────────────────────────────────────────────

def test_resolve_within_roots(tmp_path):
    from security import resolve_within_roots
    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    sibling = tmp_path / "lib-other"
    sibling.mkdir()
    assert resolve_within_roots(str(root / "a" / "x.duf"), [str(root)]) is not None
    assert resolve_within_roots(str(sibling / "x.duf"), [str(root)]) is None  # prefix trick
    assert resolve_within_roots(str(root / ".." / "x.duf"), [str(root)]) is None
    assert resolve_within_roots("a/x.duf", [str(root)]) is None
    assert resolve_within_roots(str(root / "x.duf"), []) is None
    assert resolve_within_roots("", [str(root)]) is None


# ── Demo mode without patched roots ────────────────────────────────────────────

@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/v1/scene/load", {"path": "C:/x.duf"}),
    ("POST", "/api/v1/files/reveal", {"path": "C:/x.duf"}),
    ("GET", "/api/v1/assets/thumbnail?path=C:/x.duf", None),
])
def test_demo_mode_registers_no_content_roots(authed, method, path, body):
    """Demo exposes no asset paths (assets and content-dirs are empty), so every file
    operation is out of root — it must not fall back to trusting the caller's path."""
    assert authed.request(method, path, json=body).status_code == 403
