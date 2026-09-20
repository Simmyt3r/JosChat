"""
Runs supabase/schema.sql against a real Postgres. Skipped unless
TEST_DATABASE_URL is set, e.g.

    createdb joschat_test
    TEST_DATABASE_URL=postgresql://localhost/joschat_test pytest tests/test_schema_integration.py

WARNING: the target database's `public` schema is DROPPED and recreated, so the
database name must contain "test".
"""

import os
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "supabase" / "schema.sql").read_text()
STUB = (ROOT / "tests" / "sql" / "supabase_stub.sql").read_text()

U1 = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def db():
    assert "test" in URL.rsplit("/", 1)[-1].split("?")[0].lower(), "refusing to wipe a non-test database"
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("drop schema public cascade; create schema public; drop schema if exists auth cascade;")
    cur.execute(STUB)
    cur.execute(SCHEMA)
    yield cur
    conn.close()


def test_schema_can_be_applied_twice(db):
    db.execute(SCHEMA)


def test_chain_is_valid_and_independent_of_session_timezone(db):
    db.execute("select idx from add_block('m1')")
    db.execute("set timezone = 'Africa/Lagos'")
    db.execute("select idx from add_block('m2')")
    db.execute("set timezone = 'America/Los_Angeles'")
    db.execute("select is_valid, blocks_checked from validate_chain()")
    assert db.fetchone() == (True, 2)


def test_tampering_is_detected(db):
    db.execute("select idx from add_block('m1')")
    db.execute("select idx from add_block('m2')")
    db.execute("update blocks set message_hash = 'TAMPERED' where index = 1")
    db.execute("select is_valid, invalid_index from validate_chain()")
    assert db.fetchone() == (False, 1)


def test_conversation_verification_flags_tampered_block(db):
    db.execute("insert into auth.users(id, email) values (%s, 'a@x.com')", (U1,))
    db.execute("insert into profiles(id, username) values (%s, 'alice')", (U1,))
    db.execute("insert into conversations(created_by) values (%s) returning id", (U1,))
    conv = db.fetchone()[0]
    db.execute("select idx, block_hash from add_block('m1')")
    idx, block_hash = db.fetchone()
    db.execute(
        "insert into messages(conversation_id, sender_id, encrypted_content, block_index, block_hash) "
        "values (%s, %s, 'c', %s, %s)", (conv, U1, idx, block_hash))
    db.execute("select verified from validate_conversation(%s)", (conv,))
    assert db.fetchone() == (True,)
    db.execute("update blocks set message_hash = 'TAMPERED' where index = %s", (idx,))
    db.execute("select verified from validate_conversation(%s)", (conv,))
    assert db.fetchone() == (False,)


@pytest.mark.parametrize("role", ["anon", "authenticated", "public"])
def test_browser_roles_cannot_call_chain_functions(db, role):
    for fn in ("add_block(text)", "validate_chain()", "validate_conversation(bigint)"):
        db.execute("select has_function_privilege(%s, %s, 'execute')", (role, fn))
        assert db.fetchone() == (False,), f"{role} can execute {fn}"
