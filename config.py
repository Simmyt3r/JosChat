"""
config.py
---------
Centralised configuration, loaded from environment variables.

These exact variable names match what Vercel's Supabase integration
auto-injects into your project (Vercel Dashboard -> Project -> Settings
-> Environment Variables, or `vercel env pull` to get them locally).
No SUPABASE_SERVICE_KEY is required: privileged data operations go
through a direct Postgres connection (POSTGRES_URL) instead of the
Supabase REST API, which is why there's no service-role key in this list.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Flask
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
    DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    # Supabase Auth (used ONLY for sign-up/sign-in/token verification —
    # all other data access goes through direct Postgres, see below).
    SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    # Supabase has renamed "anon key" -> "publishable key" on newer
    # projects; accept either so this works regardless of which naming
    # your project's dashboard shows.
    SUPABASE_ANON_KEY = (
        os.environ.get("SUPABASE_ANON_KEY")
        or os.environ.get("SUPABASE_PUBLISHABLE_KEY")
        or os.environ.get("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY")
    )

    # Postgres (direct connection — this is how the Flask backend reads
    # and writes profiles/conversations/messages/blocks/calls, and how it
    # calls the add_block()/validate_chain() SQL functions). Connecting
    # with these credentials uses Supabase's `postgres` role, which
    # bypasses Row Level Security by default — RLS in schema.sql still
    # protects anything the *browser* touches directly (Realtime, or a
    # future direct-from-client Supabase call), it's just not the
    # mechanism Flask itself relies on.
    #
    # POSTGRES_URL is the pooled (PgBouncer) connection — use it for
    # normal request-time queries, since Vercel functions are short-lived
    # and spin up often. POSTGRES_URL_NON_POOLING is a direct connection,
    # better suited to one-off scripts/migrations than to steady request
    # traffic.
    POSTGRES_URL = os.environ.get("POSTGRES_URL")
    POSTGRES_URL_NON_POOLING = os.environ.get("POSTGRES_URL_NON_POOLING")
    POSTGRES_HOST = os.environ.get("POSTGRES_HOST")
    POSTGRES_DATABASE = os.environ.get("POSTGRES_DATABASE")
    POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD")

    # Cloudinary
    CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME")
    CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY")
    CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET")

    # Misc
    MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB upload ceiling
    ALLOWED_MEDIA_EXTENSIONS = {
        "png", "jpg", "jpeg", "gif", "webp",
        "mp4", "mov", "webm",
        "mp3", "wav", "ogg", "m4a",
    }

    @classmethod
    def db_dsn(cls) -> str:
        """The connection string Flask should actually use at request time."""
        dsn = cls.POSTGRES_URL or cls.POSTGRES_URL_NON_POOLING
        if not dsn:
            raise RuntimeError(
                "Neither POSTGRES_URL nor POSTGRES_URL_NON_POOLING is set. "
                "Copy them from Vercel's Supabase integration or Supabase "
                "Project Settings -> Database -> Connection string."
            )
        return dsn

    @classmethod
    def validate(cls):
        missing = []
        if not cls.SUPABASE_URL:
            missing.append("SUPABASE_URL")
        if not cls.SUPABASE_ANON_KEY:
            missing.append("SUPABASE_ANON_KEY (or SUPABASE_PUBLISHABLE_KEY / NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY)")
        if not (cls.POSTGRES_URL or cls.POSTGRES_URL_NON_POOLING):
            missing.append("POSTGRES_URL (or POSTGRES_URL_NON_POOLING)")
        for name in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
            if not getattr(cls, name):
                missing.append(name)
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Copy .env.example to .env and fill in your Supabase/Cloudinary values."
            )

