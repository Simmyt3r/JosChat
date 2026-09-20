"""
routes/auth.py
--------------
Thin wrapper around Supabase Auth for sign-up/sign-in/sign-out/token
verification (those need Supabase's Auth/GoTrue service — there's no
direct-SQL equivalent that handles password hashing, email confirmation,
and JWT issuance correctly). The resulting `profiles` row, however, is
read/written with a direct Postgres query, same as every other table in
this app.

POST /api/auth/register  { username, email, password, phone_number?, public_key? }
POST /api/auth/login     { email, password }
POST /api/auth/refresh   { refresh_token }
POST /api/auth/logout    (Authorization: Bearer <token>)
GET  /api/auth/me        (Authorization: Bearer <token>)
POST /api/auth/profile   { username, phone_number?, public_key? }  (token only)

Notes on Supabase's behaviour that this file has to cope with:

* With "Confirm email" ON (the default on new projects) POST /signup answers
  with the bare user object and NO session; with it OFF it answers with
  {access_token, ..., user: {...}}. Both shapes are handled.
* Signing up with an already-registered email does NOT error when "Confirm
  email" is ON — it returns an obfuscated user whose `identities` list is
  empty. That is treated as "email already registered".
* An auth account can exist without a `profiles` row (e.g. sign-up succeeded
  but the profile insert failed). Logging in still works for such accounts;
  the client is told `profile: null` and finishes setup via POST
  /api/auth/profile instead of being locked out for good.
"""

import re

import psycopg2.errors
from flask import Blueprint, request, jsonify, g

from extensions import get_supabase_auth, db_cursor, SupabaseAuthError
from utils.auth_helpers import require_auth, require_token

auth_bp = Blueprint("auth", __name__)

USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,30}")
EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _auth_failure(exc: SupabaseAuthError, default_status: int = 400,
                  default_message: str = None, hide_detail: bool = False):
    """Translate a Supabase Auth failure into an honest HTTP response.

    Infrastructure problems (unreachable, bad API key, rate limit, email not
    confirmed) always get their own accurate answer. Anything else falls
    through to `default_status`; with hide_detail=True the caller's generic
    `default_message` is used instead of Supabase's wording (login uses this
    so a wrong password and an unknown email look identical).
    """
    message = (str(exc).strip() or default_message or "Authentication request failed")
    lower = message.lower()
    code = (exc.code or "").lower() if isinstance(exc.code, str) else ""

    if exc.status is None or (exc.status and exc.status >= 500):
        return jsonify({"error": f"Authentication service unavailable: {message}"}), 502
    # A bad/missing project API key is server configuration, not user input.
    if "api key" in lower or "apikey" in lower or exc.status == 401:
        return jsonify({"error": "Authentication service is misconfigured: invalid Supabase API key"}), 500
    if code == "email_not_confirmed" or "not confirmed" in lower:
        return jsonify({
            "error": "Please confirm your email address first — check your inbox for the confirmation link.",
            "code": "email_not_confirmed",
        }), 403
    if code == "user_already_exists" or "already registered" in lower or "already been registered" in lower:
        return jsonify({"error": "An account with this email already exists"}), 409
    if code in ("over_email_send_rate_limit", "over_request_rate_limit") or exc.status == 429:
        return jsonify({"error": "Too many attempts. Please wait a minute and try again."}), 429
    return jsonify({"error": default_message if hide_detail and default_message else message}), default_status


def _session_payload(result: dict, profile):
    return {
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token"),
        "expires_at": result.get("expires_at"),
        "profile": dict(profile) if profile else None,
    }


def _validate_username(username: str):
    if not USERNAME_RE.fullmatch(username or ""):
        return "username must be 3-30 characters and use only letters, numbers, _, . or -"
    return None


@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    phone_number = (data.get("phone_number") or "").strip() or None
    public_key = data.get("public_key")  # client-generated E2EE public key

    if not username or not email or not password:
        return jsonify({"error": "username, email, and password are required"}), 400
    error = _validate_username(username)
    if error:
        return jsonify({"error": error}), 400
    if not EMAIL_RE.fullmatch(email):
        return jsonify({"error": "Enter a valid email address"}), 400
    # Supabase's default password minimum is 6 characters. Do not impose a
    # stricter, undocumented Flask-only rule that makes otherwise valid
    # Supabase registrations fail with HTTP 400.
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400

    with db_cursor() as cur:
        cur.execute("SELECT id FROM profiles WHERE username = %s", (username,))
        if cur.fetchone():
            return jsonify({"error": "That username is already taken"}), 409
        if phone_number:
            cur.execute("SELECT id FROM profiles WHERE phone_number = %s", (phone_number,))
            if cur.fetchone():
                return jsonify({"error": "That phone number is already registered"}), 409

    try:
        signup = get_supabase_auth().sign_up(email, password)
    except SupabaseAuthError as exc:
        return _auth_failure(exc, 400, "Supabase rejected the registration request")

    # Both response shapes: {"user": {...}, "access_token": ...} (auto-confirm)
    # or the bare user object (email confirmation required).
    user = signup.get("user") if isinstance(signup.get("user"), dict) else signup
    if not user or not user.get("id"):
        return jsonify({"error": "Registration failed: unexpected response from Supabase Auth"}), 502
    if user.get("identities") == []:
        return jsonify({"error": "An account with this email already exists"}), 409

    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO profiles (id, username, phone_number, public_key, role, status)
                VALUES (%s, %s, %s, %s, 'user', 'active')
                """,
                (user["id"], username, phone_number, public_key),
            )
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "That username or phone number is already taken"}), 409

    confirmation_required = not signup.get("access_token")
    return jsonify({
        "message": (
            "Registration successful. Check your email to confirm your account, then log in."
            if confirmation_required else
            "Registration successful. You can log in now."
        ),
        "confirmation_required": confirmation_required,
        "user_id": user["id"],
    }), 201


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "email and password are required"}), 400

    try:
        result = get_supabase_auth().sign_in_with_password(email, password)
    except SupabaseAuthError as exc:
        # Wrong password / unknown email → the same generic 401 for both.
        return _auth_failure(exc, 401, "Invalid email or password", hide_detail=True)

    if not result.get("access_token") or not (result.get("user") or {}).get("id"):
        return jsonify({"error": "Invalid email or password"}), 401

    with db_cursor() as cur:
        cur.execute("SELECT * FROM profiles WHERE id = %s", (result["user"]["id"],))
        profile = cur.fetchone()

    if profile and profile.get("status") == "suspended":
        return jsonify({"error": "This account has been suspended"}), 403

    return jsonify(_session_payload(result, profile)), 200


@auth_bp.route("/refresh", methods=["POST"])
def refresh():
    data = request.get_json(silent=True) or {}
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        return jsonify({"error": "refresh_token is required"}), 400

    try:
        result = get_supabase_auth().refresh_session(refresh_token)
    except SupabaseAuthError as exc:
        if exc.status is None or exc.status >= 500:
            return _auth_failure(exc)
        return jsonify({"error": "Session expired — please log in again"}), 401

    if not result.get("access_token") or not (result.get("user") or {}).get("id"):
        return jsonify({"error": "Session expired — please log in again"}), 401

    with db_cursor() as cur:
        cur.execute("SELECT * FROM profiles WHERE id = %s", (result["user"]["id"],))
        profile = cur.fetchone()

    if profile and profile.get("status") == "suspended":
        return jsonify({"error": "This account has been suspended"}), 403

    return jsonify(_session_payload(result, profile)), 200


@auth_bp.route("/logout", methods=["POST"])
@require_token
def logout():
    try:
        get_supabase_auth().sign_out(g.token)
    except Exception:
        pass  # token may already be expired/invalid — logout is idempotent
    return jsonify({"message": "Logged out"}), 200


@auth_bp.route("/me", methods=["GET"])
@require_auth
def me():
    return jsonify({"profile": g.profile}), 200


@auth_bp.route("/profile", methods=["POST"])
@require_token
def create_profile():
    """Finish setup for an authenticated account that has no profile row."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    phone_number = (data.get("phone_number") or "").strip() or None
    public_key = data.get("public_key")

    error = _validate_username(username)
    if error:
        return jsonify({"error": error}), 400

    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM profiles WHERE id = %s", (g.user["id"],))
        if cur.fetchone():
            return jsonify({"error": "This account already has a profile"}), 409

    try:
        with db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO profiles (id, username, phone_number, public_key, role, status)
                VALUES (%s, %s, %s, %s, 'user', 'active')
                RETURNING *
                """,
                (g.user["id"], username, phone_number, public_key),
            )
            profile = cur.fetchone()
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "That username or phone number is already taken"}), 409

    return jsonify({"profile": dict(profile)}), 201
