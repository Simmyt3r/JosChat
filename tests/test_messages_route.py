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
    def _send(conversation_id, rows):
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
            json={"conversation_id": conversation_id, "encrypted_content": CIPHERTEXT},
            headers={"Authorization": "Bearer t"},
        )
        return res, route_cursors[0]
    return _send


def test_the_block_records_the_sender_and_the_conversation(send):
    rows = [(1,), {"idx": 7, "block_hash": "h" * 64}, {"id": 1, "conversation_id": 42}]
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
