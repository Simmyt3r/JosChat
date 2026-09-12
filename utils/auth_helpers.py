"""
utils/auth_helpers.py
----------------------
Every protected route expects an `Authorization: Bearer <access_token>`
header, where <access_token> is the JWT Supabase issued when the user
logged in (see routes/auth.py). We verify it by asking Supabase Auth who
it belongs to (not by decoding the JWT ourselves), then look up their
`profiles` row with a direct Postgres query.
"""

from functools import wraps
from flask import request, jsonify, g

from extensions import supabase_auth, db_cursor


def _extract_bearer_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return auth_header.split(" ", 1)[1].strip()


def get_current_user(token: str):
    """Returns the Supabase auth user for a token, or None if invalid."""
    try:
        response = supabase_auth.auth.get_user(token)
        return response.user
    except Exception:
        return None


def require_auth(f):
    """Attaches g.user (Supabase auth user) and g.profile (profiles row)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = _extract_bearer_token()
        if not token:
            return jsonify({"error": "Missing or malformed Authorization header"}), 401

        user = get_current_user(token)
        if user is None:
            return jsonify({"error": "Invalid or expired session token"}), 401

        with db_cursor() as cur:
            cur.execute("SELECT * FROM profiles WHERE id = %s", (user.id,))
            profile = cur.fetchone()

        if not profile:
            return jsonify({"error": "No profile found for this account"}), 404
        if profile.get("status") == "suspended":
            return jsonify({"error": "This account has been suspended"}), 403

        g.user = user
        g.profile = dict(profile)
        g.token = token
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    """Stack this under @require_auth to additionally require role='admin'."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if g.profile.get("role") != "admin":
            return jsonify({"error": "Admin privileges required"}), 403
        return f(*args, **kwargs)
    return wrapper
