"""
routes/media.py
----------------
POST /api/media/upload  (multipart/form-data, field name: "file")

Uploads to Cloudinary and returns the secure URL + public_id, which the
client then attaches to a /api/messages/send call. Media is uploaded to
a per-conversation folder so it's easy to manage/delete later (e.g. if a
conversation is deleted).

NOTE ON ENCRYPTION: for true end-to-end encryption of attachments, the
client should encrypt the file's bytes *before* uploading here, and
decrypt them again after downloading from the returned URL. This route
does not itself encrypt anything — it only handles storage.
"""

import uuid
from flask import Blueprint, request, jsonify, g
import cloudinary.uploader

from config import Config
from extensions import db_cursor
from utils.auth_helpers import require_auth

media_bp = Blueprint("media", __name__)


def _is_participant(conversation_id, user_id) -> bool:
    with db_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, user_id),
        )
        return cur.fetchone() is not None


def _allowed_file(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in Config.ALLOWED_MEDIA_EXTENSIONS


@media_bp.route("/upload", methods=["POST"])
@require_auth
def upload_media():
    if "file" not in request.files:
        return jsonify({"error": "No file part in the request"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not _allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type"}), 415

    conversation_id = request.form.get("conversation_id", "")
    if not conversation_id.isdigit():
        return jsonify({"error": "conversation_id (integer) is required"}), 400
    if not _is_participant(int(conversation_id), g.profile["id"]):
        return jsonify({"error": "Not a participant in this conversation"}), 403
    folder = f"joschat/{int(conversation_id)}"
    public_id = f"{uuid.uuid4().hex}"

    try:
        result = cloudinary.uploader.upload(
            file,
            folder=folder,
            public_id=public_id,
            resource_type="auto",   # auto-detects image / video / raw (audio)
            overwrite=False,
        )
    except Exception as exc:
        return jsonify({"error": f"Upload failed: {exc}"}), 502

    return jsonify({
        "secure_url": result.get("secure_url"),
        "public_id": result.get("public_id"),
        "resource_type": result.get("resource_type"),
        "bytes": result.get("bytes"),
        "format": result.get("format"),
    }), 201


@media_bp.route("/<path:public_id>", methods=["DELETE"])
@require_auth
def delete_media(public_id):
    # Assets live under joschat/<conversation_id>/…; only participants of that
    # conversation may delete them (not any logged-in user, and not assets
    # outside this app's folder).
    parts = public_id.split("/")
    if len(parts) < 3 or parts[0] != "joschat" or not parts[1].isdigit():
        return jsonify({"error": "Not found"}), 404
    if not _is_participant(int(parts[1]), g.profile["id"]):
        return jsonify({"error": "Not a participant in this conversation"}), 403
    try:
        result = cloudinary.uploader.destroy(public_id, invalidate=True)
    except Exception as exc:
        return jsonify({"error": f"Delete failed: {exc}"}), 502
    return jsonify(result), 200
