"""POST /api/messages/send against a mocked Postgres and mocked Supabase Auth."""

from contextlib import contextmanager

import pytest

from blockchain import hash_message

USER_ID = "11111111-1111-1111-1111-111111111111"
CIPHERTEXT = "bm90IHJlYWxseSBjaXBoZXJ0ZXh0"


class ScriptedCursor:
    """Returns the queued rows, in order, to successive fetchone() calls."""

    def __init__(self, rows):
        self.rows, self.executed = list(rows), []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeAuth:
    def get_user(self, token):
        return {"id": USER_ID}


@pytest.fixture()
def send(monkeypatch, client):
    def _send(conversation_id, rows, **extra):
        route_cursors = []

        @contextmanager
        def route_db(commit=False):
            cur = ScriptedCursor(rows)
            route_cursors.append(cur)
            yield cur

        @contextmanager
        def auth_db(commit=False):
            yield ScriptedCursor([{"id": USER_ID, "username": "alice", "role": "user", "status": "active"}])

        monkeypatch.setattr("utils.auth_helpers.get_supabase_auth", lambda: FakeAuth())
        monkeypatch.setattr("utils.auth_helpers.db_cursor", auth_db)
        monkeypatch.setattr("routes.messages.db_cursor", route_db)
        res = client.post(
            "/api/messages/send",
            json={"conversation_id": conversation_id, "encrypted_content": CIPHERTEXT, **extra},
            headers={"Authorization": "Bearer t"},
        )
        return res, route_cursors[0]
    return _send


def test_the_block_records_the_sender_and_the_conversation(send):
    rows = [(1,), {"idx": 7, "block_hash": "h" * 64}, {"id": 1, "conversation_id": 42}, {"verified": True}]
    res, cur = send(42, rows)
    assert res.status_code == 201

    (sql, params), = [e for e in cur.executed if "add_block" in e[0]]
    assert "add_block(%s, %s::uuid, %s)" in sql
    assert params == (hash_message(CIPHERTEXT), USER_ID, 42)     # ciphertext hash, sender, conversation
    assert CIPHERTEXT not in params                                # plaintext/ciphertext itself is never chained


def test_a_non_participant_cannot_append_a_block(send):
    res, cur = send(42, [None])        # participant check finds nothing
    assert res.status_code == 403
    assert not any("add_block" in sql for sql, _ in cur.executed)


def test_replay_returns_existing_message_without_another_block(send):
    client_id = "33333333-3333-3333-3333-333333333333"
    existing = {"id": 4, "conversation_id": 42, "encrypted_content": CIPHERTEXT,
                "encrypted_media": None, "media_url": None, "media_public_id": None,
                "block_hash": "h", "block_index": 0, "verified": True}
    res, cur = send(42, [(1,), existing], client_message_id=client_id)
    assert res.status_code == 200 and res.get_json()["duplicate"] is True
    assert res.get_json()["message"]["verified"] is True
    assert not any("add_block" in sql for sql, _ in cur.executed)


def test_reused_client_id_with_different_content_is_rejected(send):
    res, cur = send(42, [(1,), {"conversation_id": 43}],
                    client_message_id="33333333-3333-3333-3333-333333333333")
    assert res.status_code == 409
    assert not any("add_block" in sql for sql, _ in cur.executed)


def test_new_client_id_is_stored_and_verified(send):
    client_id = "33333333-3333-3333-3333-333333333333"
    res, cur = send(42, [(1,), None, {"idx": 0, "block_hash": "h"}, {"id": 1}, {"verified": True}],
                    client_message_id=client_id)
    assert res.status_code == 201 and res.get_json()["message"]["verified"] is True
    inserts = [params for sql, params in cur.executed if "INSERT INTO messages" in sql]
    assert inserts[0][-1] == client_id
    assert any("pg_advisory_xact_lock" in sql for sql, _ in cur.executed)
