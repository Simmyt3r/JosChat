"""Group creation validates participants and inserts everyone atomically."""
from contextlib import contextmanager

import pytest

ME = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
CAROL = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def wire(monkeypatch):
    class Auth:
        def get_user(self, token):
            return {"id": ME}

    class Cursor:
        def __init__(self):
            self.sql = ""
            self.writes = []
        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
            self.writes.append((self.sql, params))
        def executemany(self, sql, params):
            self.writes.append(("participants", params))
        def fetchone(self):
            if "profiles" in self.sql:
                return {"id": ME, "username": "alice", "role": "user", "status": "active"}
            return {"id": 9, "is_group": True, "title": "Team"}
        def fetchall(self):
            return [{"id": BOB, "username": "bob"}, {"id": CAROL, "username": "carol"}]

    cursors = []
    @contextmanager
    def db(commit=False):
        cur = Cursor(); cursors.append(cur); yield cur
    monkeypatch.setattr("utils.auth_helpers.get_supabase_auth", lambda: Auth())
    monkeypatch.setattr("utils.auth_helpers.db_cursor", db)
    monkeypatch.setattr("routes.conversations.db_cursor", db)
    return cursors


def test_group_creation_includes_caller_and_distinct_members(client, wire):
    res = client.post("/api/conversations", headers={"Authorization": "Bearer t"},
                      json={"is_group": True, "title": " Team ", "participant_ids": [BOB, CAROL, BOB, ME]})
    assert res.status_code == 201
    inserts = [p for c in wire for sql, p in c.writes if sql.startswith("INSERT INTO conversations")]
    assert inserts == [(True, "Team", ME)]
    members = [p for c in wire for sql, p in c.writes if sql == "participants"][0]
    assert set(members) == {(9, ME), (9, BOB), (9, CAROL)}


@pytest.mark.parametrize("body", [
    {"is_group": "false"},
    {"is_group": True, "title": ""},
    {"is_group": True, "title": 42},
    {"is_group": True, "title": "x" * 81},
    {"is_group": True, "title": "Team", "participant_ids": [BOB]},
    {"is_group": True, "title": "Team", "participant_ids": [BOB] * 50},
])
def test_invalid_groups_create_no_conversation(client, wire, body):
    assert client.post("/api/conversations", headers={"Authorization": "Bearer t"}, json=body).status_code == 400
    assert not any(sql.startswith("INSERT") for c in wire for sql, _ in c.writes)
