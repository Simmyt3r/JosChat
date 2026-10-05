"""
GET /api/conversations: besides each conversation, the response now also
carries what the chat list needs to render previews and an integrity
indicator without a round trip per row (dissertation Section 4.1.6-adjacent
"chat list with previews and integrity indicator").

Two layers:
  - DB-level tests (skipped unless TEST_DATABASE_URL is set) exercise the
    real SQL in routes/conversations.py against a real Postgres.
  - A route-level unit test checks the JSON the Flask route hands back is
    exactly what the (mocked) query returned, and that auth still gates it.
"""

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "supabase" / "schema.sql").read_text()
STUB = (ROOT / "tests" / "sql" / "supabase_stub.sql").read_text()

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
CAROL = "33333333-3333-3333-3333-333333333333"

LIST_QUERY = """
SELECT c.*,
       COALESCE((
           SELECT array_agg(pr.username ORDER BY pr.username)
           FROM conversation_participants cp2
           JOIN profiles pr ON pr.id = cp2.user_id
           WHERE cp2.conversation_id = c.id AND cp2.user_id <> %s
       ), ARRAY[]::text[]) AS other_usernames,
       (
           SELECT pr.public_key
           FROM conversation_participants cp3
           JOIN profiles pr ON pr.id = cp3.user_id
           WHERE cp3.conversation_id = c.id AND cp3.user_id <> %s AND NOT c.is_group
       ) AS other_public_key,
       lm.id AS last_message_id,
       lm.sender_id AS last_message_sender_id,
       lm.encrypted_content AS last_message_ciphertext,
       lm.created_at AS last_message_at,
       COALESCE(iv.all_verified, true) AS integrity_ok
FROM conversations c
JOIN conversation_participants cp ON cp.conversation_id = c.id
LEFT JOIN LATERAL (
    SELECT id, sender_id, encrypted_content, created_at
    FROM messages m
    WHERE m.conversation_id = c.id
    ORDER BY m.created_at DESC
    LIMIT 1
) lm ON true
LEFT JOIN LATERAL (
    SELECT bool_and(verified) AS all_verified FROM validate_conversation(c.id)
) iv ON true
WHERE cp.user_id = %s
ORDER BY COALESCE(lm.created_at, c.created_at) DESC
"""


@pytest.fixture()
def db():
    assert "test" in URL.rsplit("/", 1)[-1].split("?")[0].lower(), "refusing to wipe a non-test database"
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("drop schema if exists private cascade; drop schema public cascade; create schema public; drop schema if exists auth cascade;")
    cur.execute(STUB)
    cur.execute(SCHEMA)
    yield cur
    conn.close()


def _send(db, conv, sender, ciphertext="c"):
    db.execute("select idx, block_hash from add_block(%s, %s, %s)", ("h-" + ciphertext, sender, conv))
    idx, block_hash = db.fetchone()
    db.execute(
        "insert into messages(conversation_id, sender_id, encrypted_content, block_index, block_hash) "
        "values (%s, %s, %s, %s, %s) returning id", (conv, sender, ciphertext, idx, block_hash))
    return db.fetchone()[0], idx


def _list_as(db, user_id):
    db.execute(LIST_QUERY, (user_id, user_id, user_id))
    cols = [c.name for c in db.description]
    return {row[cols.index("id")]: dict(zip(cols, row)) for row in db.fetchall()}


@pytestmark_db
def test_direct_conversation_carries_the_other_persons_public_key(db):
    db.execute("insert into auth.users(id) values (%s), (%s)", (ALICE, BOB))
    db.execute("insert into profiles(id, username, public_key) values (%s, 'alice', 'pub-a'), (%s, 'bob', 'pub-b')", (ALICE, BOB))
    db.execute("insert into conversations(created_by) values (%s) returning id", (ALICE,))
    conv = db.fetchone()[0]
    db.execute("insert into conversation_participants(conversation_id, user_id) values (%s, %s), (%s, %s)",
               (conv, ALICE, conv, BOB))

    rows = _list_as(db, ALICE)
    assert rows[conv]["other_public_key"] == "pub-b"
    assert rows[conv]["other_usernames"] == ["bob"]


@pytestmark_db
def test_group_conversation_has_no_single_other_public_key(db):
    db.execute("insert into auth.users(id) values (%s), (%s), (%s)", (ALICE, BOB, CAROL))
    db.execute(
        "insert into profiles(id, username, public_key) values (%s, 'alice', 'pub-a'), (%s, 'bob', 'pub-b'), (%s, 'carol', 'pub-c')",
        (ALICE, BOB, CAROL),
    )
    db.execute("insert into conversations(is_group, created_by) values (true, %s) returning id", (ALICE,))
    conv = db.fetchone()[0]
    db.execute(
        "insert into conversation_participants(conversation_id, user_id) values (%s, %s), (%s, %s), (%s, %s)",
        (conv, ALICE, conv, BOB, conv, CAROL),
    )

    rows = _list_as(db, ALICE)
    assert rows[conv]["other_public_key"] is None
    assert sorted(rows[conv]["other_usernames"]) == ["bob", "carol"]


@pytestmark_db
def test_last_message_fields_reflect_the_newest_message(db):
    db.execute("insert into auth.users(id) values (%s), (%s)", (ALICE, BOB))
    db.execute("insert into profiles(id, username) values (%s, 'alice'), (%s, 'bob')", (ALICE, BOB))
    db.execute("insert into conversations(created_by) values (%s) returning id", (ALICE,))
    conv = db.fetchone()[0]
    db.execute("insert into conversation_participants(conversation_id, user_id) values (%s, %s), (%s, %s)",
               (conv, ALICE, conv, BOB))

    _send(db, conv, ALICE, "first")
    newest_id, _ = _send(db, conv, BOB, "second")

    row = _list_as(db, ALICE)[conv]
    assert row["last_message_id"] == newest_id
    assert row["last_message_sender_id"] == BOB
    assert row["last_message_ciphertext"] == "second"


@pytestmark_db
def test_conversation_with_no_messages_has_null_last_message_and_is_verified(db):
    db.execute("insert into auth.users(id) values (%s), (%s)", (ALICE, BOB))
    db.execute("insert into profiles(id, username) values (%s, 'alice'), (%s, 'bob')", (ALICE, BOB))
    db.execute("insert into conversations(created_by) values (%s) returning id", (ALICE,))
    conv = db.fetchone()[0]
    db.execute("insert into conversation_participants(conversation_id, user_id) values (%s, %s), (%s, %s)",
               (conv, ALICE, conv, BOB))

    row = _list_as(db, ALICE)[conv]
    assert row["last_message_id"] is None
    assert row["integrity_ok"] is True


@pytestmark_db
def test_integrity_ok_flips_false_when_a_block_is_tampered(db):
    db.execute("insert into auth.users(id) values (%s), (%s)", (ALICE, BOB))
    db.execute("insert into profiles(id, username) values (%s, 'alice'), (%s, 'bob')", (ALICE, BOB))
    db.execute("insert into conversations(created_by) values (%s) returning id", (ALICE,))
    conv = db.fetchone()[0]
    db.execute("insert into conversation_participants(conversation_id, user_id) values (%s, %s), (%s, %s)",
               (conv, ALICE, conv, BOB))
    _, idx = _send(db, conv, ALICE)

    assert _list_as(db, ALICE)[conv]["integrity_ok"] is True

    db.execute("update blocks set sender_id = %s where index = %s", (BOB, idx))
    assert _list_as(db, ALICE)[conv]["integrity_ok"] is False


@pytestmark_db
def test_conversations_are_ordered_by_most_recent_activity_not_creation(db):
    db.execute("insert into auth.users(id) values (%s), (%s)", (ALICE, BOB))
    db.execute("insert into profiles(id, username) values (%s, 'alice'), (%s, 'bob')", (ALICE, BOB))
    db.execute("insert into conversations(created_by) values (%s) returning id", (ALICE,))
    older = db.fetchone()[0]
    db.execute("insert into conversations(created_by) values (%s) returning id", (ALICE,))
    newer_but_quiet = db.fetchone()[0]
    for conv in (older, newer_but_quiet):
        db.execute("insert into conversation_participants(conversation_id, user_id) values (%s, %s), (%s, %s)",
                   (conv, ALICE, conv, BOB))

    # The OLDER conversation gets a message after the newer one was created,
    # so it should sort first -- activity, not creation time, decides order.
    _send(db, older, ALICE)

    db.execute(LIST_QUERY, (ALICE, ALICE, ALICE))
    ordered_ids = [row[0] for row in db.fetchall()]
    assert ordered_ids[0] == older
    assert ordered_ids[1] == newer_but_quiet


# --- route-level: auth + JSON pass-through --------------------------------

import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("POSTGRES_URL", "postgres://user:pass@localhost:5432/postgres")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "demo")
os.environ.setdefault("CLOUDINARY_API_KEY", "key")
os.environ.setdefault("CLOUDINARY_API_SECRET", "secret")


class ScriptedCursor:
    def __init__(self, rows):
        self.rows, self.executed = list(rows), []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        row = self.rows.pop(0) if self.rows else []
        return row if isinstance(row, list) else [row]


class FakeAuth:
    def get_user(self, token):
        return {"id": ALICE}


@pytest.fixture()
def client():
    from app import create_app
    return create_app().test_client()


def test_route_passes_through_the_new_fields_and_still_requires_auth(monkeypatch, client):
    row = {
        "id": 1, "is_group": False, "other_usernames": ["bob"], "other_public_key": "pub-b",
        "last_message_id": 9, "last_message_sender_id": BOB, "last_message_ciphertext": "e2.abc",
        "last_message_at": "2026-01-01T00:00:00Z", "integrity_ok": False,
    }

    route_cursors = []

    @contextmanager
    def route_db(commit=False):
        cur = ScriptedCursor([[row]])
        route_cursors.append(cur)
        yield cur

    @contextmanager
    def auth_db(commit=False):
        yield ScriptedCursor([{"id": ALICE, "username": "alice", "role": "user", "status": "active"}])

    monkeypatch.setattr("utils.auth_helpers.get_supabase_auth", lambda: FakeAuth())
    monkeypatch.setattr("utils.auth_helpers.db_cursor", auth_db)
    monkeypatch.setattr("routes.conversations.db_cursor", route_db)

    res = client.get("/api/conversations", headers={"Authorization": "Bearer t"})
    assert res.status_code == 200
    assert res.get_json()["conversations"] == [row]

    assert client.get("/api/conversations").status_code == 401
