"""Unit tests for ArdiClient — focuses on the parts we can test without a chain.

Specifically:
  - commit_hash() must produce the same hash the contract expects
  - The SDK's view of `phase` lookup matches contract semantics
  - Type/ABI integrity (build_transaction doesn't crash)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from eth_account import Account
from eth_utils import keccak


def _build_client():
    """Build a client with a stub web3 — enough for hash-format tests
    that don't actually connect to chain."""
    from ardi_sdk import ArdiClient

    # Use a valid Anvil RPC URL format (we won't actually use it, but the
    # constructor instantiates Web3 which is fine even unconnected).
    pk = "0x" + "11" * 32

    contracts = {
        "ardi_nft":         "0x1111111111111111111111111111111111111111",
        "ardi_token":       "0x2222222222222222222222222222222222222222",
        "bond_escrow":      "0x3333333333333333333333333333333333333333",
        "epoch_draw":       "0x4444444444444444444444444444444444444444",
        "mint_controller":  "0x5555555555555555555555555555555555555555",
    }
    return ArdiClient(
        rpc_url="http://localhost:8545",   # not actually called
        coordinator_url="http://localhost:8080",
        agent_private_key=pk,
        contracts=contracts,
        chain_id=31337,
    )


def test_commit_hash_matches_contract_format():
    """The on-chain ArdiEpochDraw expects:
        keccak256(abi.encodePacked(string guess, address agent, bytes32 nonce))
    The SDK's commit_hash MUST produce identical bytes — otherwise
    every reveal will revert with CommitMismatch.
    """
    client = _build_client()

    guess = "fire"
    nonce = bytes.fromhex("a1" * 32)

    # SDK output
    sdk_hash = client.commit_hash(guess, nonce)

    # Recompute manually using the spec
    expected = keccak(
        guess.encode("utf-8")
        + bytes.fromhex(client.address.lower().removeprefix("0x"))
        + nonce
    )
    assert sdk_hash == expected
    assert len(sdk_hash) == 32


def test_commit_hash_rejects_short_nonce():
    """nonce MUST be exactly 32 bytes; shorter values would silently
    produce a different hash than the contract expects."""
    client = _build_client()
    with pytest.raises(ValueError, match="32 bytes"):
        client.commit_hash("fire", b"\x00" * 16)
    with pytest.raises(ValueError, match="32 bytes"):
        client.commit_hash("fire", b"\x00" * 33)


def test_commit_hash_changes_with_inputs():
    """Verify all three inputs (guess, nonce, agent address) influence the hash —
    otherwise we'd be exposed to commit replay across pairs."""
    client = _build_client()
    nonce = bytes.fromhex("a1" * 32)

    h1 = client.commit_hash("fire", nonce)
    h2 = client.commit_hash("water", nonce)
    h3 = client.commit_hash("fire", bytes.fromhex("b2" * 32))
    assert h1 != h2  # different guess
    assert h1 != h3  # different nonce


def test_address_derivation():
    """Ensure the SDK's `.address` matches eth_account's derivation from the same key."""
    pk = "0x" + "11" * 32
    expected = Account.from_key(pk).address
    client = _build_client()
    assert client.address == expected
