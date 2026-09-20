from blockchain import Blockchain, hash_message


def _chain_with_messages():
    chain = Blockchain()
    for text in ("hello", "how are you?", "meet at 5pm"):
        chain.add_block(hash_message(text))
    return chain


def test_fresh_chain_is_valid():
    valid, bad_index = _chain_with_messages().validate_chain()
    assert valid is True and bad_index is None


def test_tampering_with_a_block_is_detected():
    chain = _chain_with_messages()
    chain.chain[2].message_hash = "TAMPERED"
    valid, bad_index = chain.validate_chain()
    assert valid is False and bad_index == 2


def test_relinking_a_block_is_detected():
    chain = _chain_with_messages()
    chain.chain[2].previous_hash = "0" * 64
    valid, bad_index = chain.validate_chain()
    assert valid is False and bad_index == 2


def test_hash_message_is_deterministic_sha256():
    assert hash_message("abc") == hash_message("abc")
    assert hash_message("abc") != hash_message("abd")
    assert len(hash_message("abc")) == 64
