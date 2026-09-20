"""
Registration/login against a mocked Supabase Auth + mocked Postgres.

The Supabase responses below are the real shapes GoTrue produces:
  * "Confirm email" ON  -> POST /signup returns the bare user object, no session
  * "Confirm email" OFF -> POST /signup returns {access_token, ..., user: {...}}
  * signing up an already-registered email with confirmation ON returns an
    obfuscated user with an EMPTY `identities` list (not an error).
"""

from contextlib import contextmanager

import pytest

from extensions import SupabaseAuthError

USER_ID = "11111111-1111-1111-1111-111111111111"
NEW_USER = {"id": USER_ID, "email": "a@example.com", "identities": [{"identity_id": "x"}]}


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeAuth:
    def __init__(self, signup=None, signin=None, error=None):
        self._signup, self._signin, self._error = signup, signin, error

    def sign_up(self, email, password):
        if self._error:
            raise self._error
        return self._signup

    def sign_in_with_password(self, email, password):
        if self._error:
            raise self._error
        return self._signin


@pytest.fixture()
def wire(monkeypatch):
    """Patch routes.auth's collaborators; returns the list of cursors used."""
    def _wire(auth, fetch_rows=()):
        cursors = []

        @contextmanager
        def fake_db_cursor(commit=False):
            cur = FakeCursor(fetch_rows)
            cursors.append(cur)
            yield cur

        monkeypatch.setattr("routes.auth.get_supabase_auth", lambda: auth)
        monkeypatch.setattr("routes.auth.db_cursor", fake_db_cursor)
        return cursors
    return _wire


REGISTER = {"username": "alice", "email": "a@example.com", "password": "secret1"}


def _inserted_profile(cursors):
    return [c for cur in cursors for c in cur.executed if c[0].startswith("INSERT INTO profiles")]


def test_register_with_email_confirmation_on(client, wire):
    cursors = wire(FakeAuth(signup=NEW_USER))          # bare user object, no session
    r = client.post("/api/auth/register", json=REGISTER)
    assert r.status_code == 201
    assert r.get_json()["confirmation_required"] is True
    assert r.get_json()["user_id"] == USER_ID
    assert _inserted_profile(cursors)[0][1][0] == USER_ID


def test_register_with_email_confirmation_off(client, wire):
    cursors = wire(FakeAuth(signup={"access_token": "t", "user": NEW_USER}))
    r = client.post("/api/auth/register", json=REGISTER)
    assert r.status_code == 201
    assert r.get_json()["confirmation_required"] is False
    assert _inserted_profile(cursors)


def test_register_existing_email_with_confirmation_on_is_409_and_creates_no_profile(client, wire):
    cursors = wire(FakeAuth(signup={"id": USER_ID, "email": "a@example.com", "identities": []}))
    r = client.post("/api/auth/register", json=REGISTER)
    assert r.status_code == 409
    assert not _inserted_profile(cursors)


def test_register_existing_email_error_is_409(client, wire):
    wire(FakeAuth(error=SupabaseAuthError("User already registered", status=422, code="user_already_exists")))
    assert client.post("/api/auth/register", json=REGISTER).status_code == 409


def test_register_bad_api_key_is_a_500_not_a_validation_error(client, wire):
    wire(FakeAuth(error=SupabaseAuthError("Invalid API key", status=401)))
    r = client.post("/api/auth/register", json=REGISTER)
    assert r.status_code == 500 and "misconfigured" in r.get_json()["error"]


def test_register_supabase_unreachable_is_502(client, wire):
    wire(FakeAuth(error=SupabaseAuthError("Supabase Auth is unreachable: timed out")))
    assert client.post("/api/auth/register", json=REGISTER).status_code == 502


def test_register_username_taken_is_409(client, wire):
    wire(FakeAuth(signup=NEW_USER), fetch_rows=[{"id": "someone"}])
    assert client.post("/api/auth/register", json=REGISTER).status_code == 409


@pytest.mark.parametrize("payload", [
    {"username": "al", "email": "a@example.com", "password": "secret1"},        # username too short
    {"username": "bad name", "email": "a@example.com", "password": "secret1"},  # illegal characters
    {"username": "alice", "email": "not-an-email", "password": "secret1"},
    {"username": "alice", "email": "a@example.com", "password": "12345"},       # < 6 chars
    {},
])
def test_register_validation(client, wire, payload):
    wire(FakeAuth(signup=NEW_USER))
    assert client.post("/api/auth/register", json=payload).status_code == 400


LOGIN = {"email": "a@example.com", "password": "secret1"}
SESSION = {"access_token": "at", "refresh_token": "rt", "expires_at": 1, "user": {"id": USER_ID}}


def test_login_ok_returns_profile(client, wire):
    wire(FakeAuth(signin=SESSION), fetch_rows=[{"id": USER_ID, "username": "alice", "status": "active"}])
    r = client.post("/api/auth/login", json=LOGIN)
    assert r.status_code == 200
    assert r.get_json()["access_token"] == "at"
    assert r.get_json()["profile"]["username"] == "alice"


def test_login_wrong_password_is_401_generic(client, wire):
    wire(FakeAuth(error=SupabaseAuthError("Invalid login credentials", status=400, code="invalid_credentials")))
    r = client.post("/api/auth/login", json=LOGIN)
    assert r.status_code == 401 and r.get_json()["error"] == "Invalid email or password"


def test_login_unconfirmed_email_says_so(client, wire):
    wire(FakeAuth(error=SupabaseAuthError("Email not confirmed", status=400, code="email_not_confirmed")))
    r = client.post("/api/auth/login", json=LOGIN)
    assert r.status_code == 403 and r.get_json()["code"] == "email_not_confirmed"


def test_login_bad_api_key_is_not_reported_as_wrong_password(client, wire):
    wire(FakeAuth(error=SupabaseAuthError("Invalid API key", status=401)))
    assert client.post("/api/auth/login", json=LOGIN).status_code == 500


def test_login_account_without_profile_can_still_sign_in(client, wire):
    wire(FakeAuth(signin=SESSION), fetch_rows=[])   # no profiles row
    r = client.post("/api/auth/login", json=LOGIN)
    assert r.status_code == 200 and r.get_json()["profile"] is None


def test_login_suspended_account_is_403(client, wire):
    wire(FakeAuth(signin=SESSION), fetch_rows=[{"id": USER_ID, "username": "alice", "status": "suspended"}])
    assert client.post("/api/auth/login", json=LOGIN).status_code == 403
