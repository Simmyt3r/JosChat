"""POST /api/messages/send with an attachment: the new encrypted_media field.

encrypted_media carries ciphertext of {url, public_id, ...} — sealed client-side
the same way message text is — so the plaintext location of an attachment is not
sitting in the message row. This route never sees or needs the plaintext; it only
bounds the size of what it's given. See README, "Encrypted media".
"""

from contextlib import contextmanager

import pytest

from blockchain import hash_message
from routes.messages import MAX_ENCRYPTED_MEDIA_CHARS

USER_ID = "11111111-1111-1111-1111-111111111111"
CIPHERTEXT = "bm90IHJlYWxseSBjaXBoZXJ0ZXh0"
ENCRYPTED_MEDIA = "e2." + "QQ==" * 20   # stands in for real ciphertext; the route treats it opaquely


class ScriptedCursor:
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
    def _send(body, rows=None):
        rows = rows if rows is not None else [(1,), {"idx": 7, "block_hash": "h" * 64}, {"id": 1, "conversation_id": 42}]
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
        res = client.post("/api/messages/send", json=body, headers={"Authorization": "Bearer t"})
        return res, (route_cursors[0] if route_cursors else None)
    return _send


def _insert(cur):
    (sql, params), = [e for e in cur.executed if e[0].startswith("INSERT INTO messages")]
    return sql, params


def test_a_message_with_an_encrypted_attachment_is_accepted_and_stored(send):
    res, cur = send({"conversation_id": 42, "encrypted_content": CIPHERTEXT, "encrypted_media": ENCRYPTED_MEDIA})
    assert res.status_code == 201
    sql, params = _insert(cur)
    assert "encrypted_media" in sql
    assert ENCRYPTED_MEDIA in params


def test_a_text_only_message_stores_no_media_reference(send):
    res, cur = send({"conversation_id": 42, "encrypted_content": CIPHERTEXT})
    assert res.status_code == 201
    _, params = _insert(cur)
    assert None in params and ENCRYPTED_MEDIA not in params


def test_the_server_never_needs_or_sees_the_plaintext_url(send):
    """The route only bounds the ciphertext's size; it cannot and does not parse it."""
    res, cur = send({"conversation_id": 42, "encrypted_content": CIPHERTEXT, "encrypted_media": ENCRYPTED_MEDIA})
    assert res.status_code == 201
    blob = str(cur.executed)
    assert "cloudinary" not in blob.lower() and "res.cloudinary.com" not in blob


@pytest.mark.parametrize("bad, why", [
    (123, "not a string"),
    ([], "not a string"),
    ("", "empty"),
    ("x" * (MAX_ENCRYPTED_MEDIA_CHARS + 1), "too long"),
])
def test_a_malformed_encrypted_media_field_is_rejected(send, bad, why):
    res, cur = send({"conversation_id": 42, "encrypted_content": CIPHERTEXT, "encrypted_media": bad})
    assert res.status_code == 400, why
    assert cur is None or not any("INSERT INTO messages" in sql for sql, _ in cur.executed)


def test_right_at_the_size_limit_is_still_accepted(send):
    res, _ = send({"conversation_id": 42, "encrypted_content": CIPHERTEXT, "encrypted_media": "x" * MAX_ENCRYPTED_MEDIA_CHARS})
    assert res.status_code == 201


def test_legacy_plaintext_media_url_still_works_for_an_older_or_other_client(send):
    """The bundled client never sends these any more, but the field is still accepted."""
    res, cur = send({
        "conversation_id": 42, "encrypted_content": CIPHERTEXT,
        "media_url": "https://res.cloudinary.com/demo/image/upload/v1/x.png", "media_public_id": "joschat/42/x",
    })
    assert res.status_code == 201
    _, params = _insert(cur)
    assert "https://res.cloudinary.com/demo/image/upload/v1/x.png" in params


def test_a_bogus_plaintext_media_url_is_still_rejected(send):
    res, cur = send({"conversation_id": 42, "encrypted_content": CIPHERTEXT, "media_url": "https://evil.example/x.png"})
    assert res.status_code == 400
    assert cur is None or not any("INSERT INTO messages" in sql for sql, _ in cur.executed)


def test_an_attachment_message_still_chains_only_its_text_hash(send):
    """The block's hash covers encrypted_content only; the attachment reference is not chained."""
    res, cur = send({"conversation_id": 42, "encrypted_content": CIPHERTEXT, "encrypted_media": ENCRYPTED_MEDIA})
    assert res.status_code == 201
    (_, params), = [e for e in cur.executed if "add_block" in e[0]]
    assert params == (hash_message(CIPHERTEXT), USER_ID, 42)
