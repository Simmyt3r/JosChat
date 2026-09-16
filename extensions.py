"""
extensions.py
-------------
Shared client/connection helpers, created once and imported wherever
needed (routes, utils, etc.) — the standard Flask pattern for avoiding
circular imports.

Two separate things live here, deliberately kept apart:

  1. `supabase_auth` — a Supabase client authenticated with the anon/
     publishable key, used ONLY for auth.sign_up / sign_in_with_password /
     get_user / sign_out. Those operations must go through Supabase's
     Auth (GoTrue) service; there's no direct-SQL equivalent that does
     password hashing, email confirmation, JWT issuance, etc. correctly.

  2. `get_db_connection()` / `db_cursor()` — a direct psycopg2 connection
     to the same Postgres database, used for EVERYTHING else (profiles,
     conversations, messages, blocks, calls). This connects as the
     `postgres` role from your Vercel/Supabase Postgres credentials,
     which bypasses Row Level Security — appropriate here because Flask
     itself is the trusted server-side layer doing its own authorization
     checks (see utils/auth_helpers.py) before ever running a query.
"""

import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from urllib.parse import urlparse, unquote
import cloudinary
import json
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from config import Config

class SupabaseAuthError(RuntimeError):
    pass


class SupabaseAuthClient:
    """Minimal Supabase Auth REST client.

    Using the HTTP API directly avoids coupling startup to a particular
    supabase-py key parser. It accepts both legacy JWT anon keys and the newer
    opaque ``sb_publishable_...`` keys supported by Supabase.
    """

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, path: str, method="GET", payload=None, token=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"apikey": self.api_key, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urlrequest.Request(
            f"{self.base_url}/auth/v1{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlrequest.urlopen(req, timeout=15) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw)
                message = detail.get("msg") or detail.get("message") or detail.get("error_description") or raw
            except Exception:
                message = raw or str(exc)
            raise SupabaseAuthError(message) from exc
        except URLError as exc:
            raise SupabaseAuthError(f"Supabase Auth is unreachable: {exc.reason}") from exc

    def sign_up(self, email: str, password: str):
        return self._request("/signup", "POST", {"email": email, "password": password})

    def sign_in_with_password(self, email: str, password: str):
        return self._request(
            "/token?grant_type=password", "POST", {"email": email, "password": password}
        )

    def get_user(self, token: str):
        return self._request("/user", "GET", token=token)

    def sign_out(self, token: str):
        return self._request("/logout", "POST", token=token)


_supabase_auth = None


def get_supabase_auth() -> SupabaseAuthClient:
    global _supabase_auth
    if _supabase_auth is None:
        if not Config.SUPABASE_URL or not Config.SUPABASE_ANON_KEY:
            raise RuntimeError(
                "Supabase Auth is not configured. Set SUPABASE_URL and "
                "SUPABASE_ANON_KEY (or SUPABASE_PUBLISHABLE_KEY / "
                "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY)."
            )
        _supabase_auth = SupabaseAuthClient(Config.SUPABASE_URL, Config.SUPABASE_ANON_KEY)
    return _supabase_auth


def init_cloudinary():
    cloudinary.config(
        cloud_name=Config.CLOUDINARY_CLOUD_NAME,
        api_key=Config.CLOUDINARY_API_KEY,
        api_secret=Config.CLOUDINARY_API_SECRET,
        secure=True,
    )


def _parse_postgres_url(url: str) -> dict:
    """
    Break a postgres:// URL into its parts ourselves using the standard
    library, instead of handing the raw string to psycopg2.

    Why: psycopg2.connect(dsn, sslmode=..., cursor_factory=...) internally
    calls its own make_dsn()/parse_dsn() to merge the extra keyword args
    into the DSN string, and that internal parser can choke on otherwise
    valid query strings that Supabase/PgBouncer/Vercel produce (observed:
    "invalid dsn: invalid URI query parameter" on a working, valid URL).
    Parsing with urllib.parse and passing explicit keyword arguments to
    psycopg2.connect() avoids that code path entirely.
    """
    parsed = urlparse(url)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "dbname": (parsed.path or "/postgres").lstrip("/") or "postgres",
    }


def get_db_connection():
    """A fresh connection per call — appropriate for short-lived
    serverless function invocations. sslmode=require matches Supabase's
    Postgres requirements."""
    params = _parse_postgres_url(Config.db_dsn())
    conn = psycopg2.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params["password"],
        dbname=params["dbname"],
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    conn.autocommit = False
    return conn


@contextmanager
def db_cursor(commit: bool = False):
    """
    Usage:
        with db_cursor() as cur:
            cur.execute("SELECT * FROM profiles WHERE id = %s", (user_id,))
            row = cur.fetchone()

        with db_cursor(commit=True) as cur:
            cur.execute("INSERT INTO messages (...) VALUES (...) RETURNING *")
            new_row = cur.fetchone()
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
