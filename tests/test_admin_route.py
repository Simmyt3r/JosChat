"""
routes/admin.py: every route here must refuse anyone who isn't an admin
(including a suspended admin), then do what it says once they are one.
"""

from contextlib import contextmanager

import pytest

ADMIN = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_ADMIN = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PLAIN_USER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
MISSING = "dddddddd-dddd-dddd-dddd-dddddddddddd"

ADMIN_ROUTES = [
    ("get", "/api/admin/users", None),
    ("post", f"/api/admin/users/{OTHER_ADMIN}/suspend", None),
    ("post", f"/api/admin/users/{OTHER_ADMIN}/reinstate", None),
    ("get", "/api/admin/blockchain/validate", None),
    ("get", "/api/admin/conversations", None),
    ("get", "/api/admin/conversations/1/validate", None),
    ("get", "/api/admin/stats", None),
]


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


def _profile(user_id, role="user", status="active"):
    return {"id": user_id, "username": f"u-{user_id[:4]}", "role": role, "status": status}


@pytest.fixture()
def admin_as(monkeypatch, client):
    """admin_as(user_id, role, status)(method, path, rows, json=...) -> (response, cursor)"""
    def make(user_id, role="admin", status="active"):
        def _do(method, path, rows, json=None):
            route_cursors = []

            @contextmanager
            def route_db(commit=False):
                cur = ScriptedCursor(rows)
                route_cursors.append(cur)
                yield cur

            @contextmanager
            def auth_db(commit=False):
                yield ScriptedCursor([_profile(user_id, role, status)])

            monkeypatch.setattr("utils.auth_helpers.get_supabase_auth", lambda: FakeAuth(user_id))
            monkeypatch.setattr("utils.auth_helpers.db_cursor", auth_db)
            monkeypatch.setattr("routes.admin.db_cursor", route_db)
            fn = getattr(client, method)
            res = fn(path, json=json, headers={"Authorization": "Bearer t"})
            return res, (route_cursors[0] if route_cursors else None)
        return _do
    return make


# --- access control, across every route -------------------------------------

@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_a_plain_user_is_refused(admin_as, method, path, body):
    res, cur = admin_as(PLAIN_USER, role="user")(method, path, [], json=body)
    assert res.status_code == 403
    assert cur is None or not cur.executed


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES)
def test_a_suspended_admin_is_still_refused(admin_as, method, path, body):
    res, cur = admin_as(ADMIN, role="admin", status="suspended")(method, path, [], json=body)
    assert res.status_code == 403
    assert cur is None or not cur.executed


# --- users -------------------------------------------------------------------

def test_listing_users(admin_as):
    rows = [[_profile(PLAIN_USER), _profile(ADMIN, role="admin")]]
    res, cur = admin_as(ADMIN)("get", "/api/admin/users", rows)
    assert res.status_code == 200
    assert len(res.get_json()["users"]) == 2


def test_suspending_a_user(admin_as):
    rows = [_profile(OTHER_ADMIN, role="user", status="suspended")]
    res, cur = admin_as(ADMIN)("post", f"/api/admin/users/{OTHER_ADMIN}/suspend", rows)
    assert res.status_code == 200
    assert res.get_json()["user"]["status"] == "suspended"
    assert any("UPDATE profiles" in sql for sql, _ in cur.executed)


def test_suspending_a_missing_user_is_404(admin_as):
    res, cur = admin_as(ADMIN)("post", f"/api/admin/users/{MISSING}/suspend", [None])
    assert res.status_code == 404


def test_an_admin_cannot_suspend_themself(admin_as):
    res, cur = admin_as(ADMIN)("post", f"/api/admin/users/{ADMIN}/suspend", [])
    assert res.status_code == 400
    assert cur is None or not cur.executed


def test_reinstating_a_user(admin_as):
    rows = [_profile(OTHER_ADMIN, role="user", status="active")]
    res, cur = admin_as(ADMIN)("post", f"/api/admin/users/{OTHER_ADMIN}/reinstate", rows)
    assert res.status_code == 200
    assert res.get_json()["user"]["status"] == "active"


def test_reinstating_a_missing_user_is_404(admin_as):
    res, cur = admin_as(ADMIN)("post", f"/api/admin/users/{MISSING}/reinstate", [None])
    assert res.status_code == 404


# --- conversations ------------------------------------------------------------

def test_listing_conversations(admin_as):
    rows = [[
        {"id": 1, "is_group": False, "created_at": "2026-01-01T00:00:00Z",
         "participants": ["alice", "bob"], "message_count": 12},
        {"id": 2, "is_group": True, "created_at": "2026-01-02T00:00:00Z",
         "participants": ["alice", "carol", "dave"], "message_count": 4},
    ]]
    res, cur = admin_as(ADMIN)("get", "/api/admin/conversations", rows)
    assert res.status_code == 200
    body = res.get_json()["conversations"]
    assert len(body) == 2 and body[0]["message_count"] == 12


# --- blockchain audits ---------------------------------------------------------

def test_full_chain_audit_reports_a_valid_chain(admin_as):
    rows = [{"is_valid": True, "invalid_index": None, "blocks_checked": 42}]
    res, cur = admin_as(ADMIN)("get", "/api/admin/blockchain/validate", rows)
    body = res.get_json()
    assert res.status_code == 200
    assert body["is_valid"] is True and body["blocks_checked"] == 42


def test_full_chain_audit_reports_tampering(admin_as):
    rows = [{"is_valid": False, "invalid_index": 7, "blocks_checked": 42}]
    res, cur = admin_as(ADMIN)("get", "/api/admin/blockchain/validate", rows)
    body = res.get_json()
    assert body["is_valid"] is False and body["invalid_index"] == 7


def test_full_chain_audit_on_an_empty_chain(admin_as):
    res, cur = admin_as(ADMIN)("get", "/api/admin/blockchain/validate", [None])
    body = res.get_json()
    assert res.status_code == 200
    assert body == {"is_valid": True, "invalid_index": None, "blocks_checked": 0}


def test_conversation_audit_all_clear(admin_as):
    rows = [
        [{"1": 1}],  # existence check
        [
            {"message_id": 1, "verified": True},
            {"message_id": 2, "verified": True},
        ],
    ]
    res, cur = admin_as(ADMIN)("get", "/api/admin/conversations/9/validate", rows)
    body = res.get_json()
    assert res.status_code == 200
    assert body["conversation_id"] == 9
    assert body["messages_checked"] == 2
    assert body["all_verified"] is True
    assert body["flagged_message_ids"] == []


def test_conversation_audit_flags_tampered_messages(admin_as):
    rows = [
        [{"1": 1}],
        [
            {"message_id": 1, "verified": True},
            {"message_id": 2, "verified": False},
        ],
    ]
    res, cur = admin_as(ADMIN)("get", "/api/admin/conversations/9/validate", rows)
    body = res.get_json()
    assert body["all_verified"] is False
    assert body["flagged_message_ids"] == [2]


def test_conversation_audit_on_a_missing_conversation_is_404(admin_as):
    res, cur = admin_as(ADMIN)("get", "/api/admin/conversations/999/validate", [None])
    assert res.status_code == 404


# --- stats ---------------------------------------------------------------------

def test_platform_stats_assembles_every_counter(admin_as):
    rows = [
        {"n": 10}, {"n": 8}, {"n": 2}, {"n": 5}, {"n": 100}, {"n": 300}, {"n": 7},
        [{"status": "ended", "n": 5}, {"status": "missed", "n": 2}],
    ]
    res, cur = admin_as(ADMIN)("get", "/api/admin/stats", rows)
    body = res.get_json()
    assert res.status_code == 200
    assert body == {
        "total_users": 10,
        "active_users": 8,
        "suspended_users": 2,
        "total_conversations": 5,
        "total_messages": 100,
        "total_blocks": 300,
        "total_calls": 7,
        "calls_by_status": {"ended": 5, "missed": 2},
    }
