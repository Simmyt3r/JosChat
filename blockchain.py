"""
blockchain.py
-------------
A small, dependency-free, in-memory implementation of the same hash-chain
scheme used by the Supabase `add_block` / `validate_chain` SQL functions
(see supabase/schema.sql).

Why keep a Python copy at all, if Postgres does the real work?
  1. It's directly runnable/testable without a live Supabase project
     (see tests below or `python blockchain.py`), which is useful for
     demonstrating and unit-testing the algorithm in isolation.
  2. It documents, in plain Python, exactly what the SQL function does,
     since `digest(index::text || created_at::text || message_hash ||
     previous_hash, 'sha256')` is otherwise a bit opaque to read cold.

The Flask routes do NOT use this class to persist real messages — they
run `SELECT * FROM add_block(...)` directly over a Postgres connection
instead (see extensions.db_cursor / routes/messages.py), because that
executes inside a single locked Postgres transaction and is therefore
safe under concurrent requests, which a Python object living inside a
stateless, horizontally-scaled serverless function is not.
"""

import hashlib
from datetime import datetime, timezone


class Block:
    def __init__(self, index, timestamp, message_hash, previous_hash, block_hash=None):
        self.index = index
        self.timestamp = timestamp
        self.message_hash = message_hash
        self.previous_hash = previous_hash
        self.block_hash = block_hash or self.compute_hash()

    def compute_hash(self):
        payload = f"{self.index}{self.timestamp}{self.message_hash}{self.previous_hash}"
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_dict(self):
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "message_hash": self.message_hash,
            "previous_hash": self.previous_hash,
            "block_hash": self.block_hash,
        }


class Blockchain:
    GENESIS_PREVIOUS_HASH = "0" * 64

    def __init__(self, blocks=None):
        self.chain = blocks or [self._genesis_block()]

    def _genesis_block(self):
        return Block(0, self._now(), "GENESIS", self.GENESIS_PREVIOUS_HASH)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @property
    def last_block(self):
        return self.chain[-1]

    def add_block(self, message_hash: str) -> Block:
        new_block = Block(
            index=self.last_block.index + 1,
            timestamp=self._now(),
            message_hash=message_hash,
            previous_hash=self.last_block.block_hash,
        )
        self.chain.append(new_block)
        return new_block

    def validate_chain(self):
        """Returns (is_valid: bool, first_invalid_index: int | None)."""
        for i in range(1, len(self.chain)):
            current, previous = self.chain[i], self.chain[i - 1]
            if current.block_hash != current.compute_hash():
                return False, current.index
            if current.previous_hash != previous.block_hash:
                return False, current.index
        return True, None


def hash_message(encrypted_content: str) -> str:
    """Fingerprints already-encrypted message content for chaining."""
    return hashlib.sha256(encrypted_content.encode()).hexdigest()


if __name__ == "__main__":
    # Quick self-test: `python blockchain.py`
    chain = Blockchain()
    for text in ["hello", "how are you?", "meet at 5pm"]:
        chain.add_block(hash_message(text))

    valid, bad_index = chain.validate_chain()
    print(f"Chain of {len(chain.chain)} blocks valid? {valid}")

    # Now tamper with a block and confirm validation catches it.
    chain.chain[2].message_hash = "TAMPERED"
    valid, bad_index = chain.validate_chain()
    print(f"After tampering with block 2, valid? {valid} (first bad index: {bad_index})")
