"""Regression tests for the authenticated, paginated call history endpoint."""
from tests.test_calls_route import CALLER, OUTSIDER


def test_history_only_queries_own_calls(call_as):
    rows = [[{"id": 4, "caller_id": CALLER, "status": "ended"}]]
    res, cur = call_as(CALLER)("get", "/api/calls/history?limit=30&offset=0", rows)
    assert res.status_code == 200
    assert [c["id"] for c in res.get_json()["calls"]] == [4]
    sql, args = cur.executed[0]
    assert "c.caller_id = %s OR c.callee_id = %s" in sql
    assert "conversation_participants" in sql
    assert args == (CALLER, CALLER, CALLER, 30, 0)


def test_history_empty(call_as):
    res, _ = call_as(CALLER)("get", "/api/calls/history", [[]])
    assert res.status_code == 200 and res.get_json()["calls"] == []


def test_invalid_history_pagination_rejected(call_as):
    for suffix in ("?limit=0", "?limit=101", "?offset=-1", "?limit=nope"):
        res, cur = call_as(CALLER)("get", "/api/calls/history" + suffix, [])
        assert res.status_code == 400
        assert cur is None


def test_history_does_not_require_conversation_id(call_as):
    res, cur = call_as(OUTSIDER)("get", "/api/calls/history", [[]])
    assert res.status_code == 200
    assert "WHERE (c.caller_id = %s OR c.callee_id = %s)" in cur.executed[0][0]
