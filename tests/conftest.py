"""
Test configuration.

Environment variables are set BEFORE `app` is imported anywhere (config.py
reads them at import time), and unconditionally — so a developer's real .env
can never point the unit tests at a live Supabase project. The unit tests
mock the database and Supabase Auth; nothing here touches the network.
"""

import os
import sys
from pathlib import Path

os.environ.update({
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "test-anon-key",
    "POSTGRES_URL": "postgres://user:pass@localhost:5432/postgres",
    "CLOUDINARY_CLOUD_NAME": "demo",
    "CLOUDINARY_API_KEY": "key",
    "CLOUDINARY_API_SECRET": "secret",
    "FLASK_DEBUG": "false",
})

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture()
def client():
    from app import create_app
    return create_app().test_client()
