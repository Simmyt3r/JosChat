"""
routes/calls.py: starting, accepting, ending and listing calls.

This is the durable log only — see the module docstring in routes/calls.py.
The live "ringing" signal and the SDP/ICE exchange are Supabase Realtime and
never touch Flask; that side is covered by the two-browser scenario in
tests/e2e (not part of the pytest suite, since it needs a browser).
"""

from contextlib import contextmanager

import pytest

CALLER = "11111111-1111-1111-1111-111111111111"
CALLEE = "22222222-2222-2222-2222-222222222222"
OUTSIDER = "33333333-3333-3333-3333-333333333333"


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
    def __init__(self, uid):
        self.uid = uid

    def get_user(self, token):
        return {"id": self.uid}


@pytest.fixture()
def call_as(monkeypatch, client):
    """call_as(user_id)(method, path, rows, json=...) -> (response, cursor)"""
    def make(user_id):
        def _do(method, path, rows, json=None):
            route_cursors = []

            @contextmanager
            def route_db(commit=False):
                cur = ScriptedCursor(rows)
                route_cursors.append(cur)
                yield cur

            @contextmanager
            def auth_db(commit=False):
                yield ScriptedCursor([{"id": user_id, "username": "u", "role": "user", "status": "active"}])

            monkeypatch.setattr("utils.auth_helpers.get_supabase_auth", lambda: FakeAuth(user_id))
            monkeypatch.setattr("utils.auth_helpers.db_cursor", auth_db)
            monkeypatch.setattr("routes.calls.db_cursor", route_db)
            fn = getattr(client, method)
            res = fn(path, json=json, headers={"Authorization": "Bearer t"})
            return res, (route_cursors[0] if route_cursors else None)
        return _do
    return make


# --- start -------------------------------------------------------------------

def test_starting_a_call_between_two_participants_succeeds(call_as):
    rows = [(1,), (1,), None, {"id": 9, "conversation_id": 1, "caller_id": CALLER, "callee_id": CALLEE,
                               "call_type": "video", "status": "initiated"}]
    res, cur = call_as(CALLER)("post", "/api/calls/start", rows,
                                json={"conversation_id": 1, "callee_id": CALLEE, "call_type": "video"})
    assert res.status_code == 201
    assert res.get_json()["call"]["status"] == "initiated"
    assert any("INSERT INTO calls" in sql for sql, _ in cur.executed)


def test_defaults_to_a_voice_call(call_as):
    rows = [(1,), (1,), None, {"id": 9, "conversation_id": 1, "caller_id": CALLER, "callee_id": CALLEE,
                               "call_type": "voice", "status": "initiated"}]
    res, _ = call_as(CALLER)("post", "/api/calls/start", rows, json={"conversation_id": 1, "callee_id": CALLEE})
    assert res.status_code == 201 and res.get_json()["call"]["call_type"] == "voice"


def test_a_non_participant_cannot_start_a_call(call_as):
    res, cur = call_as(OUTSIDER)("post", "/api/calls/start", [None],
                                  json={"conversation_id": 1, "callee_id": CALLEE})
    assert res.status_code == 403
    assert not any("INSERT INTO calls" in sql for sql, _ in cur.executed)


def test_a_second_call_cannot_start_while_one_is_active(call_as):
    rows = [(1,), (1,), {"id": 5}]   # both participants ok, then finds an existing active call
    res, cur = call_as(CALLER)("post", "/api/calls/start", rows, json={"conversation_id": 1, "callee_id": CALLEE})
    assert res.status_code == 409
    assert not any("INSERT INTO calls" in sql for sql, _ in cur.executed)


@pytest.mark.parametrize("body, why", [
    ({"callee_id": CALLEE}, "missing conversation_id"),
    ({"conversation_id": 1}, "missing callee_id"),
    ({"conversation_id": "1", "callee_id": CALLEE}, "conversation_id not an int"),
    ({"conversation_id": True, "callee_id": CALLEE}, "booleans are not conversation ids"),
    ({"conversation_id": 1, "callee_id": "not-a-uuid"}, "callee_id not a uuid"),
    ({"conversation_id": 1, "callee_id": CALLEE, "call_type": "screen-share"}, "bad call_type"),
])
def test_malformed_start_requests_are_rejected(call_as, body, why):
    res, cur = call_as(CALLER)("post", "/api/calls/start", [], json=body)
    assert res.status_code == 400, why
    assert cur is None or not cur.executed


# --- accept --------------------------------------------------------------------

def test_the_callee_can_accept_a_ringing_call(call_as):
    rows = [{"id": 9, "status": "connected", "caller_id": CALLER, "callee_id": CALLEE}]
    res, cur = call_as(CALLEE)("post", "/api/calls/9/accept", rows)
    assert res.status_code == 200 and res.get_json()["call"]["status"] == "connected"
    (sql, params), = [e for e in cur.executed if e[0].startswith("UPDATE calls")]
    assert "ended_at" not in sql               # accepting is not the same as ending
    assert params == (9, CALLEE)


def test_the_caller_cannot_accept_their_own_call(call_as):
    res, cur = call_as(CALLER)("post", "/api/calls/9/accept", [None])
    assert res.status_code == 404


def test_a_call_cannot_be_accepted_twice(call_as):
    res, cur = call_as(CALLEE)("post", "/api/calls/9/accept", [None])   # WHERE status='initiated' matches nothing
    assert res.status_code == 404


def test_an_outsider_cannot_accept_a_call(call_as):
    res, cur = call_as(OUTSIDER)("post", "/api/calls/9/accept", [None])
    assert res.status_code == 404


# --- end -------------------------------------------------------------------------

def test_the_callee_can_decline_by_ending_the_call(call_as):
    rows = [{"id": 9, "status": "missed", "ended_at": "2026-01-01T00:00:00Z"}]
    res, _ = call_as(CALLEE)("post", "/api/calls/9/end", rows, json={"status": "missed"})
    assert res.status_code == 200 and res.get_json()["call"]["status"] == "missed"


def test_the_caller_can_hang_up(call_as):
    rows = [{"id": 9, "status": "ended", "ended_at": "2026-01-01T00:00:00Z"}]
    res, _ = call_as(CALLER)("post", "/api/calls/9/end", rows, json={"status": "ended"})
    assert res.status_code == 200


def test_ending_with_no_status_defaults_to_ended(call_as):
    rows = [{"id": 9, "status": "ended"}]
    res, cur = call_as(CALLER)("post", "/api/calls/9/end", rows, json={})
    assert res.status_code == 200
    (_, params), = [e for e in cur.executed if e[0].startswith("UPDATE calls")]
    assert params[0] == "ended"


def test_an_invalid_status_falls_back_to_ended(call_as):
    rows = [{"id": 9, "status": "ended"}]
    res, cur = call_as(CALLER)("post", "/api/calls/9/end", rows, json={"status": "not-a-real-status"})
    assert res.status_code == 200
    (_, params), = [e for e in cur.executed if e[0].startswith("UPDATE calls")]
    assert params[0] == "ended"


def test_an_outsider_cannot_end_someone_elses_call(call_as):
    res, _ = call_as(OUTSIDER)("post", "/api/calls/9/end", [None], json={"status": "ended"})
    assert res.status_code == 404


def test_ending_an_already_ended_call_is_a_clean_404_not_a_crash(call_as):
    res, _ = call_as(CALLER)("post", "/api/calls/999/end", [None], json={"status": "ended"})
    assert res.status_code == 404


# --- history -----------------------------------------------------------------------

def test_a_participant_can_read_call_history(call_as):
    rows = [(1,), [{"id": 1, "status": "ended"}, {"id": 2, "status": "missed"}]]
    res, _ = call_as(CALLER)("get", "/api/calls/1", rows)
    assert res.status_code == 200 and len(res.get_json()["calls"]) == 2


def test_a_non_participant_cannot_read_call_history(call_as):
    res, _ = call_as(OUTSIDER)("get", "/api/calls/1", [None])
    assert res.status_code == 403
