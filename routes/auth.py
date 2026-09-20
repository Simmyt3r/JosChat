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
POST /api/auth/logout    (Authorization: Bearer <token>)
GET  /api/auth/me        (Authorization: Bearer <token>)
"""

from flask import Blueprint, request, jsonify, g
import re


from extensions import get_supabase_auth, db_cursor
from utils.auth_helpers import require_auth

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    phone_number = data.get("phone_number")
    public_key = data.get("public_key")  # client-generated E2EE public key

    if not username or not email or not password:
        return jsonify({"error": "username, email, and password are required"}), 400
    if len(username) < 3 or len(username) > 30 or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        return jsonify({"error": "username must be 3-30 characters and use only letters, numbers, _, . or -"}), 400
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
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

    try:
        signup = get_supabase_auth().sign_up(email, password)
    except Exception as exc:
        message = str(exc).strip() or "Supabase rejected the registration request"
        lower = message.lower()
        if "invalid api key" in lower or "api key" in lower and "invalid" in lower:
            # This is server configuration, not bad user input. Returning 500
            # makes the real cause visible in logs/clients instead of disguising
            # it as a registration validation error.
            return jsonify({"error": "Authentication service is misconfigured: invalid Supabase API key"}), 500
        if "already registered" in lower or "already been registered" in lower:
            return jsonify({"error": "An account with this email already exists"}), 409
        return jsonify({"error": f"Registration failed: {message}"}), 400

    if not signup.get("user") or not signup["user"].get("id"):
        return jsonify({"error": "Registration failed"}), 400

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO profiles (id, username, phone_number, public_key, role, status)
            VALUES (%s, %s, %s, %s, 'user', 'active')
            """,
            (signup["user"]["id"], username, phone_number, public_key),
        )

    return jsonify({
        "message": "Registration successful. Check your email to confirm your account "
                    "if email confirmation is enabled on this Supabase project.",
        "user_id": signup["user"]["id"],
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
    except Exception:
        return jsonify({"error": "Invalid email or password"}), 401

    if not result.get("access_token"):
        return jsonify({"error": "Invalid email or password"}), 401

    with db_cursor() as cur:
        cur.execute("SELECT * FROM profiles WHERE id = %s", (result["user"]["id"],))
        profile = cur.fetchone()

    return jsonify({
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token"),
        "expires_at": result.get("expires_at"),
        "profile": dict(profile) if profile else None,
    }), 200


@auth_bp.route("/logout", methods=["POST"])
@require_auth
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
