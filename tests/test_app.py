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


def test_service_worker_served_from_root_and_never_cached(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["Service-Worker-Allowed"] == "/"
    assert r.headers["Cache-Control"] == "no-cache"
    assert "javascript" in r.headers["Content-Type"]


def test_protected_routes_require_a_token(client):
    for path in ("/api/auth/me", "/api/conversations", "/api/messages/1", "/api/admin/users"):
        assert client.get(path).status_code == 401, path


def test_missing_env_keeps_health_reachable_and_reports_why(monkeypatch):
    # A missing variable must not crash the whole function at import time.
    monkeypatch.setattr(Config, "SUPABASE_URL", None)
    from app import create_app
    c = create_app().test_client()

    health = c.get("/api/health")
    assert health.status_code == 500
    assert health.get_json()["status"] == "misconfigured"
    assert "SUPABASE_URL" in health.get_json()["detail"]

    other = c.post("/api/auth/login", json={"email": "a@b.co", "password": "secret1"})
    assert other.status_code == 500 and "misconfigured" in other.get_json()["error"]
