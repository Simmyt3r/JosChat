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

from blockchain import Block

psycopg2 = pytest.importorskip("psycopg2")

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "supabase" / "schema.sql").read_text()
STUB = (ROOT / "tests" / "sql" / "supabase_stub.sql").read_text()

U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"


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


def _conversation(db):
    """Two users in one conversation; returns the conversation id."""
    db.execute("insert into auth.users(id, email) values (%s, 'a@x.com'), (%s, 'b@x.com')", (U1, U2))
    db.execute("insert into profiles(id, username) values (%s, 'alice'), (%s, 'bob')", (U1, U2))
    db.execute("insert into conversations(created_by) values (%s) returning id", (U1,))
    conv = db.fetchone()[0]
    db.execute("insert into conversation_participants(conversation_id, user_id) values (%s, %s), (%s, %s)",
               (conv, U1, conv, U2))
    return conv


def _send(db, conv, sender, ciphertext="c"):
    """What routes/messages.py does: chain a block, then store the message row."""
    db.execute("select idx, block_hash from add_block(%s, %s, %s)", ("h-" + ciphertext, sender, conv))
    idx, block_hash = db.fetchone()
    db.execute(
        "insert into messages(conversation_id, sender_id, encrypted_content, block_index, block_hash) "
        "values (%s, %s, %s, %s, %s) returning id", (conv, sender, ciphertext, idx, block_hash))
    return db.fetchone()[0], idx


def _flagged(db, conv):
    db.execute("select message_id from validate_conversation(%s) where not verified", (conv,))
    return [r[0] for r in db.fetchall()]


def test_schema_can_be_applied_twice(db):
    db.execute(SCHEMA)


def test_chain_is_valid_and_independent_of_session_timezone(db):
    conv = _conversation(db)
    db.execute("select idx from add_block('m1', %s, %s)", (U1, conv))
    db.execute("set timezone = 'Africa/Lagos'")
    db.execute("select idx from add_block('m2', %s, %s)", (U2, conv))
    db.execute("set timezone = 'America/Los_Angeles'")
    db.execute("select is_valid, blocks_checked from validate_chain()")
    assert db.fetchone() == (True, 2)


def test_tampering_is_detected(db):
    conv = _conversation(db)
    db.execute("select idx from add_block('m1', %s, %s)", (U1, conv))
    db.execute("select idx from add_block('m2', %s, %s)", (U2, conv))
    db.execute("update blocks set message_hash = 'TAMPERED' where index = 1")
    db.execute("select is_valid, invalid_index from validate_chain()")
    assert db.fetchone() == (False, 1)


def test_conversation_verification_flags_tampered_block(db):
    conv = _conversation(db)
    _, idx = _send(db, conv, U1)
    db.execute("select verified from validate_conversation(%s)", (conv,))
    assert db.fetchone() == (True,)
    db.execute("update blocks set message_hash = 'TAMPERED' where index = %s", (idx,))
    db.execute("select verified from validate_conversation(%s)", (conv,))
    assert db.fetchone() == (False,)


# --- blocks record the sender and the conversation ---------------------------

def test_a_block_stores_the_sender_and_the_conversation(db):
    conv = _conversation(db)
    _, idx = _send(db, conv, U2)
    db.execute("select sender_id::text, conversation_id from blocks where index = %s", (idx,))
    assert db.fetchone() == (U2, conv)


def test_changing_a_blocks_recorded_sender_breaks_the_chain(db):
    conv = _conversation(db)
    _send(db, conv, U1, "a")
    _, idx = _send(db, conv, U2, "b")
    db.execute("update blocks set sender_id = %s where index = %s", (U1, idx))
    db.execute("select is_valid, invalid_index from validate_chain()")
    assert db.fetchone() == (False, idx)


def test_changing_a_blocks_recorded_conversation_breaks_the_chain(db):
    conv = _conversation(db)
    _, idx = _send(db, conv, U1)
    db.execute("update blocks set conversation_id = %s where index = %s", (conv + 1, idx))
    db.execute("select is_valid, invalid_index from validate_chain()")
    assert db.fetchone() == (False, idx)


def test_a_message_cannot_be_reattributed_to_another_sender(db):
    """Only the message row is edited, the block is untouched: the mismatch is caught."""
    conv = _conversation(db)
    msg_id, _ = _send(db, conv, U1)
    assert _flagged(db, conv) == []
    db.execute("update messages set sender_id = %s where id = %s", (U2, msg_id))
    assert _flagged(db, conv) == [msg_id]


def test_a_message_cannot_be_moved_to_another_conversation_unnoticed(db):
    conv = _conversation(db)
    db.execute("insert into conversations(created_by) values (%s) returning id", (U1,))
    other = db.fetchone()[0]
    msg_id, _ = _send(db, conv, U1)
    db.execute("update messages set conversation_id = %s where id = %s", (other, msg_id))
    assert _flagged(db, other) == [msg_id]          # it now shows up under the other conversation, and fails there


def test_add_block_refuses_a_missing_sender_or_conversation(db):
    conv = _conversation(db)
    for args in ((None, conv), (U1, None)):
        with pytest.raises(psycopg2.errors.RaiseException):
            db.execute("select * from add_block('m', %s, %s)", args)


def test_a_block_cannot_have_only_one_of_the_two_ids(db):
    _conversation(db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        db.execute(
            "insert into blocks(index, message_hash, sender_id, previous_hash, block_hash) "
            "values (0, 'm', %s, %s, 'x')", (U1, "0" * 64))


def test_python_and_sql_compute_the_same_block_hash(db):
    """blockchain.py documents the scheme; this proves it is the scheme the database really uses."""
    conv = _conversation(db)
    _send(db, conv, U1, "a")
    _send(db, conv, U2, "b")
    db.execute("set timezone = 'UTC'")
    db.execute("select index, created_at::text, message_hash, sender_id::text, conversation_id, previous_hash, block_hash "
               "from blocks order by index")
    rows = db.fetchall()
    assert len(rows) == 2
    for index, created_at, message_hash, sender, conversation, previous, stored in rows:
        assert Block(index, created_at, message_hash, previous, sender_id=sender,
                     conversation_id=conversation).compute_hash() == stored


# --- upgrading a database that already holds blocks in the original format ---

OLD_BLOCKS_TABLE = """
create table blocks (
    index bigint primary key, created_at timestamptz not null default now(),
    message_hash text not null, previous_hash text not null, block_hash text not null unique
);
"""


def test_an_existing_database_is_upgraded_without_rewriting_history(db):
    # A database as it was before sender/conversation were recorded, holding two old-format blocks.
    db.execute("drop schema public cascade; create schema public; create extension if not exists pgcrypto;")
    db.execute(OLD_BLOCKS_TABLE)
    db.execute("set timezone = 'UTC'")
    prev = "0" * 64
    for i in range(2):
        db.execute("select now()::timestamptz")
        ts = db.fetchone()[0]
        db.execute(
            "insert into blocks values (%s, %s, %s, %s, encode(digest(%s::text || %s::timestamptz::text || %s || %s, 'sha256'), 'hex')) "
            "returning block_hash", (i, ts, f"old{i}", prev, i, ts, f"old{i}", prev))
        prev = db.fetchone()[0]

    db.execute(SCHEMA)                                          # the upgrade

    db.execute("select is_valid, blocks_checked from validate_chain()")
    assert db.fetchone() == (True, 2)                           # old history still valid
    db.execute("select sender_id, conversation_id from blocks where index = 0")
    assert db.fetchone() == (None, None)                        # and untouched

    db.execute("insert into auth.users(id, email) values (%s, 'a@x.com')", (U1,))
    db.execute("insert into profiles(id, username) values (%s, 'alice')", (U1,))
    db.execute("insert into conversations(created_by) values (%s) returning id", (U1,))
    conv = db.fetchone()[0]
    db.execute("select idx from add_block('new', %s, %s)", (U1, conv))
    db.execute("select is_valid, blocks_checked from validate_chain()")
    assert db.fetchone() == (True, 3)                           # old and new blocks chain together


@pytest.mark.parametrize("role", ["anon", "authenticated", "public"])
def test_browser_roles_cannot_call_chain_functions(db, role):
    for fn in ("add_block(text, uuid, bigint)", "compute_block_hash(bigint, timestamptz, text, uuid, bigint, text)",
               "validate_chain()", "validate_conversation(bigint)"):
        db.execute("select has_function_privilege(%s, %s, 'execute')", (role, fn))
        assert db.fetchone() == (False,), f"{role} can execute {fn}"
