from blockchain import Block, Blockchain, hash_message

ALICE, BOB = "user-alice", "user-bob"


def _chain_with_messages():
    chain = Blockchain()
    for text, sender, conv in (("hello", ALICE, 1), ("how are you?", BOB, 1), ("meet at 5pm", ALICE, 2)):
        chain.add_block(hash_message(text), sender_id=sender, conversation_id=conv)
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


def test_blocks_record_who_sent_the_message_and_where():
    block = _chain_with_messages().chain[2]
    assert (block.sender_id, block.conversation_id) == (BOB, 1)
    assert block.to_dict()["sender_id"] == BOB and block.to_dict()["conversation_id"] == 1


def test_changing_the_recorded_sender_is_detected():
    chain = _chain_with_messages()
    chain.chain[2].sender_id = ALICE            # "Bob's message was really Alice's"
    valid, bad_index = chain.validate_chain()
    assert valid is False and bad_index == 2


def test_moving_a_block_to_another_conversation_is_detected():
    chain = _chain_with_messages()
    chain.chain[1].conversation_id = 99
    valid, bad_index = chain.validate_chain()
    assert valid is False and bad_index == 1


def test_a_block_cannot_be_added_without_sender_and_conversation():
    chain = Blockchain()
    for kwargs in ({"sender_id": None, "conversation_id": 1}, {"sender_id": ALICE, "conversation_id": None}):
        try:
            chain.add_block(hash_message("x"), **kwargs)
        except ValueError:
            continue
        raise AssertionError("add_block accepted a block with a missing sender/conversation")


def test_blocks_written_before_ids_were_recorded_keep_their_original_hash():
    old = Block(1, "2026-09-20T08:00:00+00:00", "abc", "0" * 64)          # no sender / conversation
    import hashlib
    assert old.block_hash == hashlib.sha256(("1" + "2026-09-20T08:00:00+00:00" + "abc" + "0" * 64).encode()).hexdigest()


def test_new_format_uses_delimiters_so_fields_cannot_run_together():
    a = Block(1, "t", "m", "p", sender_id="s", conversation_id=12)
    b = Block(1, "t", "m", "p", sender_id="s1", conversation_id=2)
    assert a.block_hash != b.block_hash


def test_hash_message_is_deterministic_sha256():
    assert hash_message("abc") == hash_message("abc")
    assert hash_message("abc") != hash_message("abd")
    assert len(hash_message("abc")) == 64
