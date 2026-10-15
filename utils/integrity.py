"""Payload hashing and receipts signed with a key held outside Postgres.

Receipts protect against a database-only rewrite. The service operator who
controls both the signing key and the database remains a trust boundary.
"""
import hashlib
import hmac
import re

from config import Config


def signatures_equal(actual, expected):
    return isinstance(actual, str) and bool(re.fullmatch(r"[0-9a-f]{64}", actual)) and hmac.compare_digest(actual, expected)


def payload_hash(content, media=None, url=None, public_id=None):
    fields = [content, media, url, public_id]
    encoded = [b"-" if v is None else str(len(v.encode("utf-8"))).encode() + b":" + v.encode("utf-8") for v in fields]
    return hashlib.sha256(b"joschat/message/v2|" + b"|".join(encoded)).hexdigest()


def receipt_signature(index, block_hash, message_id, payload_digest=""):
    if not Config.LEDGER_SIGNING_KEY:
        raise RuntimeError("LEDGER_SIGNING_KEY is required to seal messages")
    data = f"joschat/receipt/v1|{index}|{block_hash}|{message_id}|{payload_digest}".encode()
    return hmac.new(Config.LEDGER_SIGNING_KEY.encode(), data, hashlib.sha256).hexdigest()


def seal_message(cur, message):
    digest = payload_hash(message["encrypted_content"], message.get("encrypted_media"), message.get("media_url"), message.get("media_public_id"))
    signature = receipt_signature(message["block_index"], message["block_hash"], message["id"], digest)
    cur.execute("INSERT INTO private.message_receipts(block_index, message_id, signature, payload_hash) VALUES (%s, %s, %s, %s)",
                (message["block_index"], message["id"], signature, digest))


def verify_receipts(cur, messages):
    """Combine SQL payload checks with receipt checks; legacy text stays readable."""
    if not messages:
        return messages
    cur.execute("SELECT block_index, message_id, signature, payload_hash FROM private.message_receipts WHERE message_id = ANY(%s)",
                ([m.get("id", m.get("message_id")) for m in messages],))
    receipts = {r["message_id"]: r for r in cur.fetchall()}
    for m in messages:
        mid = m.get("id", m.get("message_id"))
        r = receipts.get(mid)
        m["legacy_attachment"] = bool(m.get("integrity_version",1) == 1 and (m.get("encrypted_media") or m.get("media_url")))
        if r:
            digest = payload_hash(m["encrypted_content"],m.get("encrypted_media"),m.get("media_url"),m.get("media_public_id"))
            expected = receipt_signature(m["block_index"], m["block_hash"], mid, digest)
            m["verified"] = bool(m.get("verified") and r["block_index"] == m["block_index"]
                                 and signatures_equal(r["signature"], expected))
        else:
            m["verified"] = False
    return messages


def run_audit(cur, source="manual"):
    import json
    import time
    started = time.perf_counter()
    cur.execute("SELECT * FROM validate_chain()")
    report = dict(cur.fetchone())
    cur.execute("SELECT b.index, b.block_hash, m.id AS message_id, r.signature, r.message_id AS receipt_message_id, r.payload_hash, m.encrypted_content, m.encrypted_media, m.media_url, m.media_public_id, m.integrity_version "
                "FROM blocks b LEFT JOIN messages m ON m.block_index = b.index "
                "LEFT JOIN private.message_receipts r ON r.block_index = b.index ORDER BY b.index")
    rows = cur.fetchall()
    # Signed audit high-water marks detect a truncated tail even when surviving
    # rows remain internally consistent. A browser also pins previously seen heads.
    cur.execute("SELECT report FROM private.integrity_audits WHERE report ? 'checkpoint_signature' ORDER BY id DESC LIMIT 1")
    previous = cur.fetchone()
    rollback = False
    if previous:
        old = previous["report"]
        count = old.get("total_blocks") if isinstance(old, dict) else None
        shape_valid = (type(count) is int and count >= 0 and isinstance(old.get("head_hash"), str))
        if not shape_valid:
            rollback = True
        else:
            expected = receipt_signature(count, old["head_hash"], "audit")
            rollback = not signatures_equal(old.get("checkpoint_signature"), expected) or len(rows) < count
            if len(rows) >= count and count > 0:
                rollback = rollback or rows[count-1]["block_hash"] != old["head_hash"]
    bad = []
    for r in rows:
        digest = payload_hash(r["encrypted_content"] or "",r["encrypted_media"],r["media_url"],r["media_public_id"])
        expected = receipt_signature(r["index"], r["block_hash"], r["message_id"], digest)
        if r["message_id"] is None or r["receipt_message_id"] != r["message_id"] or not signatures_equal(r["signature"], expected):
            bad.append(r["index"])
    report["receipt_failures"] = bad
    report["is_valid"] = bool(report["is_valid"] and not bad and not rollback and len(rows) == report["blocks_checked"] and len(rows) >= Config.LEDGER_LEGACY_END_INDEX)
    if bad and report["invalid_index"] is None: report["invalid_index"] = bad[0]
    report["head_hash"] = rows[-1]["block_hash"] if rows else "0" * 64
    report["total_blocks"] = len(rows)
    report["checkpoint_mismatch"] = rollback
    if report["is_valid"]: report["checkpoint_signature"] = receipt_signature(len(rows), report["head_hash"], "audit")
    report["legacy_attachments"] = sum(r["integrity_version"] == 1 and bool(r["encrypted_media"] or r["media_url"]) for r in rows)
    report["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    cur.execute("INSERT INTO private.integrity_audits(source, report) VALUES (%s, %s::jsonb)", (source, json.dumps(report)))
    return report
