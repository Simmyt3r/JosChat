"""
utils/keys.py
-------------
Validation for the public key a client uploads for end-to-end encryption.

The client creates an ECDH P-256 key pair in the browser and sends ONLY the public
half, as base64 of the 65-byte uncompressed point (0x04 || X || Y). The server
never sees, needs or stores a private key, so this module also refuses anything
that looks like one: a stored private key would silently defeat the whole design.

Beyond length and format, the point is checked to actually lie on the P-256
curve. A key that is not on the curve makes every peer's key agreement fail (or,
worse, is a classic invalid-curve attack input), so it is rejected at the door.
"""

import base64
import binascii

# NIST P-256 (secp256r1) domain parameters.
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B

_MAX_ENCODED_LENGTH = 200   # 65 bytes is 88 base64 characters; anything far larger is not a key


def validate_public_key(value):
    """
    Returns (canonical_base64, None) for a valid key, (None, None) when no key was
    supplied at all, or (None, "reason") when a key was supplied but is invalid.
    """
    if value is None or value == "":
        return None, None
    if not isinstance(value, str):
        return None, "public_key must be a base64 string"

    value = value.strip()
    if len(value) > _MAX_ENCODED_LENGTH:
        return None, "public_key is too long. Send only the public key, never a private key"
    if value[:1] in ("{", "["):
        return None, "public_key must be base64 of the raw public point, not JSON. Never send a private key"

    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None, "public_key is not valid base64"

    if len(raw) != 65 or raw[0] != 0x04:
        return None, "public_key must be an uncompressed P-256 point (65 bytes)"

    x = int.from_bytes(raw[1:33], "big")
    y = int.from_bytes(raw[33:], "big")
    if not (0 < x < _P and 0 < y < _P):
        return None, "public_key is not a valid P-256 point"
    # Curve equation: y^2 = x^3 - 3x + b (mod p)
    if (y * y - (x * x * x - 3 * x + _B)) % _P != 0:
        return None, "public_key is not a valid P-256 point"

    return base64.b64encode(raw).decode("ascii"), None
