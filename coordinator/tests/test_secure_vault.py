"""Test SecureVault encryption + hash-only verification + Merkle proofs."""
import json
import tempfile
from pathlib import Path

import pytest
from eth_utils import keccak

from coordinator.secure_vault import (
    SecureVault,
    decrypt_vault_file,
    encrypt_vault_file,
)


@pytest.fixture
def tiny_vault_file():
    """Write a tiny vault.json for tests."""
    data = [
        {"word": "bitcoin", "language": "en", "riddle": "digital gold",
         "power": 100, "rarity": "legendary"},
        {"word": "fire", "language": "en", "riddle": "what burns",
         "power": 80, "rarity": "rare"},
        {"word": "比特币", "language": "zh", "riddle": "数字黄金",
         "power": 98, "rarity": "legendary"},
    ]
    p = tempfile.mktemp(suffix=".json")
    Path(p).write_text(json.dumps(data, ensure_ascii=False))
    yield p
    Path(p).unlink(missing_ok=True)


def test_encrypt_decrypt_round_trip(tiny_vault_file):
    enc_path = tempfile.mktemp(suffix=".enc")
    encrypt_vault_file(tiny_vault_file, enc_path, "secret-pass-123")
    decrypted = decrypt_vault_file(enc_path, "secret-pass-123")
    original = Path(tiny_vault_file).read_bytes()
    assert decrypted == original
    Path(enc_path).unlink()


def test_decrypt_wrong_passphrase_fails(tiny_vault_file):
    enc_path = tempfile.mktemp(suffix=".enc")
    encrypt_vault_file(tiny_vault_file, enc_path, "right-pass")

    with pytest.raises(Exception):  # cryptography raises InvalidTag
        decrypt_vault_file(enc_path, "wrong-pass")
    Path(enc_path).unlink()


def test_secure_vault_loads_plaintext(tiny_vault_file):
    """Dev mode: loads plain riddles.json."""
    v = SecureVault(tiny_vault_file)
    assert len(v) == 3


def test_secure_vault_loads_encrypted(tiny_vault_file):
    """Production mode: loads encrypted .enc with passphrase."""
    enc_path = tempfile.mktemp(suffix=".enc")
    encrypt_vault_file(tiny_vault_file, enc_path, "pass")
    v = SecureVault(enc_path, passphrase="pass")
    assert len(v) == 3
    Path(enc_path).unlink()


def test_verify_guess_correct(tiny_vault_file):
    v = SecureVault(tiny_vault_file)
    assert v.verify_guess(0, "bitcoin")
    assert v.verify_guess(0, "BITCOIN")  # case-insensitive
    assert v.verify_guess(0, "  bitcoin  ")  # trimmed
    assert v.verify_guess(2, "比特币")


def test_verify_guess_wrong(tiny_vault_file):
    v = SecureVault(tiny_vault_file)
    assert not v.verify_guess(0, "ethereum")
    assert not v.verify_guess(0, "")
    assert not v.verify_guess(99, "bitcoin")  # out of range


def test_get_public_no_answer(tiny_vault_file):
    v = SecureVault(tiny_vault_file)
    pub = v.get_public(0)
    assert "word" not in pub  # no answer leak
    assert pub["riddle"] == "digital gold"
    assert pub["power"] == 100


def test_reveal_word_audited(tiny_vault_file, caplog):
    v = SecureVault(tiny_vault_file)
    import logging

    with caplog.at_level(logging.INFO, logger="ardi.vault.audit"):
        word = v.reveal_word(0, caller="test_signer")
    assert word == "bitcoin"

    # Audit log captured the call but NOT the word
    matched = [r for r in caplog.records if "reveal_word" in r.message]
    assert len(matched) >= 1
    msg = matched[0].message
    assert "word_id=0" in msg
    assert "test_signer" in msg
    # CRITICAL: the actual answer must NEVER appear in audit log
    assert "bitcoin" not in msg.lower()


def test_reveal_stats(tiny_vault_file):
    v = SecureVault(tiny_vault_file)
    v.reveal_word(0, "test")
    v.reveal_word(1, "test")
    stats = v.reveal_stats()
    assert stats["total_reveals"] == 2
    assert stats["last_60s"] == 2


def test_drop_plaintext(tiny_vault_file):
    v = SecureVault(tiny_vault_file)
    assert v.reveal_word(0, "test") == "bitcoin"

    v.drop_plaintext()

    # Hash-verify still works
    assert v.verify_guess(0, "bitcoin")
    # But reveal raises (plaintext map empty)
    with pytest.raises(IndexError):
        v.reveal_word(0, "test")


# ----------------------------- Merkle tree -----------------------------------


def _expected_leaf(word_id: int, word: str, power: int, lang_id: int) -> bytes:
    """Mirror of ArdiEpochDraw on-chain leaf format:
    keccak256(abi.encodePacked(uint256 wordId, bytes word, uint16 power, uint8 languageId))."""
    return keccak(
        word_id.to_bytes(32, "big")
        + word.encode("utf-8")
        + power.to_bytes(2, "big")
        + lang_id.to_bytes(1, "big")
    )


def _verify_oz_proof(leaf: bytes, proof: list[bytes], root: bytes) -> bool:
    """Recompute the root by sorted-pair hashing — exactly what OZ MerkleProof.verify does."""
    cur = leaf
    for sib in proof:
        lo, hi = (cur, sib) if cur < sib else (sib, cur)
        cur = keccak(lo + hi)
    return cur == root


def test_merkle_root_and_proofs(tiny_vault_file):
    v = SecureVault(tiny_vault_file)
    root = v.merkle_root()
    assert len(root) == 32

    # Every entry must produce a valid proof
    for wid in range(len(v)):
        entry = v.get_entry(wid)
        word = v.reveal_word(wid, caller="test")
        leaf = _expected_leaf(wid, word, entry.power, entry.language_id)
        assert leaf == v.merkle_leaf(wid)

        proof = v.merkle_proof(wid)
        assert _verify_oz_proof(leaf, proof, root), f"proof for wid={wid} failed"


def test_merkle_proof_out_of_range(tiny_vault_file):
    v = SecureVault(tiny_vault_file)
    with pytest.raises(ValueError):
        v.merkle_proof(9999)
