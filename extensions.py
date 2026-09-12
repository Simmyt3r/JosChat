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
import cloudinary
from supabase import create_client, Client

from config import Config

supabase_auth: Client = create_client(Config.SUPABASE_URL, Config.SUPABASE_ANON_KEY)


def init_cloudinary():
    cloudinary.config(
        cloud_name=Config.CLOUDINARY_CLOUD_NAME,
        api_key=Config.CLOUDINARY_API_KEY,
        api_secret=Config.CLOUDINARY_API_SECRET,
        secure=True,
    )


def get_db_connection():
    """A fresh connection per call — appropriate for short-lived
    serverless function invocations. sslmode=require matches Supabase's
    Postgres requirements."""
    dsn = Config.db_dsn()
    conn = psycopg2.connect(dsn, sslmode="require", cursor_factory=psycopg2.extras.RealDictCursor)
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

