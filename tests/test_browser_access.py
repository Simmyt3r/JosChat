"""
What the BROWSER's database roles (`anon`, `authenticated`) can and cannot do.

The browser holds the public Supabase key, so anything these roles may do is
something any visitor or any registered user can do directly against the database,
bypassing the Flask backend. These tests become those roles, on a database where
new tables are granted to them by default (as Supabase does), and check the
schema takes everything back that the app does not need.

Skipped unless TEST_DATABASE_URL is set (the database name must contain "test").
"""

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "supabase" / "schema.sql").read_text()
STUB = (ROOT / "tests" / "sql" / "supabase_stub.sql").read_text()

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
CAROL = "33333333-3333-3333-3333-333333333333"
DENIED = psycopg2.errors.InsufficientPrivilege


@pytest.fixture()
def db():
    """A database with alice+bob in conversation 1 (one message) and carol alone in conversation 2."""
    assert "test" in URL.rsplit("/", 1)[-1].split("?")[0].lower(), "refusing to wipe a non-test database"
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("drop schema if exists private cascade; drop schema public cascade; create schema public; drop schema if exists auth cascade;")
    cur.execute(STUB)
    # Supabase lets the browser roles see the schemas (and call auth.uid()); without
    # this they could not even resolve table names and every denial would be for the wrong reason.
    cur.execute("grant usage on schema public to anon, authenticated; grant usage on schema auth to anon, authenticated;")
    # Supabase grants every new table to the browser roles by default; reproduce that
    # so the tests prove the schema removes it rather than never having it.
    cur.execute("alter default privileges in schema public grant all on tables to anon, authenticated;"
                "alter default privileges in schema public grant all on sequences to anon, authenticated;")
    cur.execute(SCHEMA)

    cur.execute("insert into auth.users(id, email) values (%s,'a@x.com'), (%s,'b@x.com'), (%s,'c@x.com')", (ALICE, BOB, CAROL))
    cur.execute("insert into profiles(id, username, phone_number) values (%s,'alice','+2348000000001'), (%s,'bob','+2348000000002'), (%s,'carol','+2348000000003')",
                (ALICE, BOB, CAROL))
    for creator, members in ((ALICE, (ALICE, BOB)), (CAROL, (CAROL,))):
        cur.execute("insert into conversations(created_by) values (%s) returning id", (creator,))
        conv = cur.fetchone()[0]
        for member in members:
            cur.execute("insert into conversation_participants(conversation_id, user_id) values (%s,%s)", (conv, member))
        cur.execute("select idx, block_hash from add_block(%s, %s, %s)", (f"hash-{conv}", creator, conv))
        idx, block_hash = cur.fetchone()
        cur.execute("insert into messages(conversation_id, sender_id, encrypted_content, block_index, block_hash) values (%s,%s,%s,%s,%s)",
                    (conv, creator, f"ciphertext-{conv}", idx, block_hash))
    # A block from before sender/conversation were recorded.
    cur.execute("insert into blocks(index, message_hash, previous_hash, block_hash) values (99, 'old', %s, 'legacy-block')", ("0" * 64,))
    yield cur
    conn.close()


@contextmanager
def browser(db, role="authenticated", user=None):
    """Runs the block as a browser role, with the JWT claims PostgREST/Realtime would set."""
    db.execute("select set_config('request.jwt.claim.sub', %s, false), set_config('request.jwt.claim.role', %s, false)", (user or "", role))
    db.execute(f"set role {role}")
    try:
        yield
    finally:
        db.execute("reset role")


def _rows(db, sql, *params):
    db.execute(sql, params or None)
    return db.fetchall()


# --- reading: members see their own conversations ---------------------------------

def test_a_member_can_read_their_conversation_without_policy_recursion(db):
    with browser(db, user=ALICE):
        assert len(_rows(db, "select id from messages where conversation_id = 1")) == 1
        assert {r[0] for r in _rows(db, "select user_id::text from conversation_participants where conversation_id = 1")} == {ALICE, BOB}
        assert [r[0] for r in _rows(db, "select id from conversations")] == [1]


def test_the_other_participant_sees_it_too(db):
    with browser(db, user=BOB):
        assert len(_rows(db, "select id from messages")) == 1


def test_a_user_cannot_see_conversations_they_are_not_in(db):
    with browser(db, user=CAROL):
        assert _rows(db, "select id from messages where conversation_id = 1") == []
        assert [r[0] for r in _rows(db, "select id from conversations")] == [2]
        assert _rows(db, "select user_id from conversation_participants where conversation_id = 1") == []


def test_blocks_are_visible_only_for_your_own_conversations(db):
    with browser(db, user=ALICE):
        assert [r[0] for r in _rows(db, "select conversation_id from blocks order by index")] == [1]     # not carol's, not the legacy one
    with browser(db, user=CAROL):
        assert [r[0] for r in _rows(db, "select conversation_id from blocks order by index")] == [2]


# --- writing: the browser can change nothing ---------------------------------------

def test_a_user_cannot_promote_themselves_to_admin(db):
    with browser(db, user=ALICE):
        with pytest.raises(DENIED):
            db.execute("update profiles set role = 'admin' where id = %s", (ALICE,))
    db.execute("select role from profiles where id = %s", (ALICE,))
    assert db.fetchone() == ("user",)


@pytest.mark.parametrize("statement", [
    "update profiles set status = 'suspended'",
    "update profiles set username = 'hacked'",
    "update profiles set public_key = 'x'",
    "delete from profiles",
    "insert into profiles(id, username) values (gen_random_uuid(), 'sneaky')",
    "insert into messages(conversation_id, sender_id, encrypted_content, block_index, block_hash) values (1, '%s', 'forged', 0, 'x')" % ALICE,
    "update messages set encrypted_content = 'edited'",
    "delete from messages",
    "insert into blocks(index, message_hash, previous_hash, block_hash) values (500, 'm', 'p', 'b')",
    "update blocks set message_hash = 'tampered'",
    "delete from blocks",
    "insert into conversations(created_by) values ('%s')" % ALICE,
    "insert into conversation_participants(conversation_id, user_id) values (2, '%s')" % ALICE,
    "insert into calls(conversation_id, caller_id, callee_id, call_type) values (1, '%s', '%s', 'voice')" % (ALICE, BOB),
])
def test_the_browser_cannot_write_anything(db, statement):
    with browser(db, user=ALICE):
        with pytest.raises(DENIED):
            db.execute(statement)


# --- profiles: no phone numbers ------------------------------------------------------

def test_other_peoples_phone_numbers_are_not_readable(db):
    with browser(db, user=ALICE):
        with pytest.raises(DENIED):
            db.execute("select phone_number from profiles")
    with browser(db, user=ALICE):
        with pytest.raises(DENIED):
            db.execute("select * from profiles")
    with browser(db, user=ALICE):
        assert {r[0] for r in _rows(db, "select username from profiles")} == {"alice", "bob", "carol"}
        assert _rows(db, "select id, username, public_key from profiles")            # what the app actually needs


# --- visitors who are not logged in ----------------------------------------------------

@pytest.mark.parametrize("table", ["profiles", "conversations", "conversation_participants", "messages", "blocks", "calls"])
def test_anonymous_visitors_can_read_nothing(db, table):
    with browser(db, role="anon"):
        with pytest.raises(DENIED):
            db.execute(f"select * from {table}")


# --- the helper, and the backend ----------------------------------------------------------

def test_the_membership_helper_is_not_exposed_as_an_rpc(db):
    db.execute("select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname = 'is_conversation_member'")
    assert db.fetchone() == (0,)                                   # PostgREST only exposes public
    for role, expected in (("anon", False), ("authenticated", True)):
        db.execute("select has_function_privilege(%s, 'private.is_conversation_member(bigint)', 'execute')", (role,))
        assert db.fetchone() == (expected,), role
    db.execute("select has_schema_privilege('anon', 'private', 'usage')")
    assert db.fetchone() == (False,)


def test_the_backend_role_is_unaffected(db):
    """The Flask backend connects as the table owner: it must still be able to do all of it."""
    db.execute("update profiles set role = 'admin' where id = %s", (ALICE,))
    db.execute("select idx from add_block('later', %s, 1)", (ALICE,))
    db.execute("insert into messages(conversation_id, sender_id, encrypted_content, block_index, block_hash) select 1, %s, 'x', index, block_hash from blocks order by index desc limit 1", (ALICE,))
    db.execute("select role from profiles where id = %s", (ALICE,))
    assert db.fetchone() == ("admin",)                              # the backend can change roles (the admin API does)
    db.execute("select count(*) from messages")
    assert db.fetchone() == (3,)                                    # and write messages


def test_schema_can_be_applied_again_and_still_holds(db):
    db.execute(SCHEMA)
    with browser(db, user=ALICE):
        with pytest.raises(DENIED):
            db.execute("update profiles set role = 'admin'")
        assert len(_rows(db, "select id from messages")) == 1
