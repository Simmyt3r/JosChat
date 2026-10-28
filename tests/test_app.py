import pytest

from config import Config


def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"


def test_public_config_exposes_only_whats_meant_to_be_public(client):
    # The Supabase anon key and the Cloudinary cloud name are both, by design, meant
    # to be visible to the browser (every media URL already contains the cloud name);
    # nothing secret (a service-role key, an API secret) belongs in this response.
    body = client.get("/api/config").get_json()
    assert set(body) == {"supabase_url", "supabase_anon_key", "cloudinary_cloud_name"}
    assert "secret" not in str(body).lower() and "service_role" not in str(body).lower()


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Joschat" in r.data
    assert b"Good conversations." in r.data
    assert b'href="/app?mode=register"' in r.data
    assert b'id="auth-form"' not in r.data


def test_chat_app_served_separately(client):
    r = client.get("/app?mode=register")
    assert r.status_code == 200 and b'id="auth-form"' in r.data
    assert b'id="chat-shell"' in r.data


def test_admin_page_served(client):
    # The page itself carries no secrets and needs no auth to load — the
    # security boundary is the /api/admin/* routes it calls, not this page.
    r = client.get("/admin")
    assert r.status_code == 200
    assert b"admin" in r.data.lower()


def test_service_worker_served_from_root_and_never_cached(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["Service-Worker-Allowed"] == "/"
    assert r.headers["Cache-Control"] == "no-cache"
    assert "javascript" in r.headers["Content-Type"]


def test_protected_routes_require_a_token(client):
    for path in ("/api/auth/me", "/api/conversations", "/api/messages/1", "/api/admin/users"):
        assert client.get(path).status_code == 401, path


def test_missing_env_keeps_health_reachable_without_public_diagnostics(monkeypatch):
    # A missing variable must not crash the whole function at import time.
    monkeypatch.setattr(Config, "SUPABASE_URL", None)
    from app import create_app
    c = create_app().test_client()

    health = c.get("/api/health")
    assert health.status_code == 500
    assert health.get_json()["status"] == "misconfigured"
    assert "detail" not in health.get_json()

    other = c.post("/api/auth/login", json={"email": "a@b.co", "password": "secret1"})
    assert other.status_code == 500 and "misconfigured" in other.get_json()["error"]

    assert c.get("/").status_code == 200
    assert c.get("/app").status_code == 200
    assert c.get("/static/css/landing.css").status_code == 200


def test_api_responses_are_not_cached_and_have_browser_protections(client):
    for path in ('/api/config', '/api/auth/me', '/api/not-found'):
        response = client.get(path)
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        assert response.headers['X-Frame-Options'] == 'DENY'
        assert response.headers['Referrer-Policy'] == 'no-referrer'


def test_cors_defaults_to_same_origin_and_only_allows_configured_origins(monkeypatch):
    from app import create_app
    monkeypatch.setattr(Config, 'CORS_ORIGINS', [])
    c = create_app().test_client()
    assert 'Access-Control-Allow-Origin' not in c.get('/api/config', headers={'Origin': 'https://evil.test'}).headers
    monkeypatch.setattr(Config, 'CORS_ORIGINS', ['https://chat.test'])
    c = create_app().test_client()
    for origin, allowed in [('https://chat.test', True), ('https://evil.test', False)]:
        r = c.get('/api/config', headers={'Origin': origin})
        assert (r.headers.get('Access-Control-Allow-Origin') == origin) is allowed
        assert 'Access-Control-Allow-Credentials' not in r.headers


def test_debug_may_show_configuration_diagnostics(monkeypatch):
    monkeypatch.setattr(Config, 'SUPABASE_URL', None)
    monkeypatch.setattr(Config, 'DEBUG', True)
    from app import create_app
    assert 'SUPABASE_URL' in create_app().test_client().get('/api/health').get_json()['detail']
