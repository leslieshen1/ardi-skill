"""Shared OpenZeppelin-compatible Merkle utilities.

Used by both vault tree and daily settlement airdrop trees.
"""
from __future__ import annotations

from eth_utils import keccak


def hash_pair(a: bytes, b: bytes) -> bytes:
    lo, hi = (a, b) if a < b else (b, a)
    return keccak(lo + hi)


def build_levels(leaves: list[bytes]) -> list[list[bytes]]:
    """Returns levels[0] = leaves, levels[-1] = [root]."""
    if not leaves:
        return [[b"\x00" * 32]]
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        cur = levels[-1]
        nxt = []
        for i in range(0, len(cur), 2):
            if i + 1 < len(cur):
                nxt.append(hash_pair(cur[i], cur[i + 1]))
            else:
                nxt.append(cur[i])
        levels.append(nxt)
    return levels


def proof_for(levels: list[list[bytes]], index: int) -> list[bytes]:
    proof = []
    idx = index
    for level in levels[:-1]:
        sib = idx ^ 1
        if sib < len(level):
            proof.append(level[sib])
        idx //= 2
    return proof


def airdrop_leaf(account: str, amount: int) -> bytes:
    """LEGACY single-token leaf format.

    Kept only for backward-compat tests. Production code uses
    `dual_airdrop_leaf` since the AWP-aligned ArdiMintController.claim
    distributes BOTH $aArdi and AWP from a single Merkle root.
    """
    from web3 import Web3
    addr = bytes.fromhex(Web3.to_checksum_address(account)[2:])
    return keccak(addr + amount.to_bytes(32, "big"))


def dual_airdrop_leaf(account: str, ardi_amount: int, awp_amount: int) -> bytes:
    """Dual-token leaf format used by ArdiMintController.claim:
        keccak256(abi.encodePacked(account, ardiAmount, awpAmount))

    Both amounts are uint256 (32 bytes big-endian).
    """
    from web3 import Web3
    addr = bytes.fromhex(Web3.to_checksum_address(account)[2:])
    return keccak(
        addr
        + ardi_amount.to_bytes(32, "big")
        + awp_amount.to_bytes(32, "big")
    )


def build_airdrop_tree(allocations: dict[str, int]) -> tuple[bytes, dict[str, list[bytes]]]:
    """LEGACY single-token tree builder. Use `build_dual_airdrop_tree` instead."""
    items = sorted(allocations.items(), key=lambda kv: kv[0].lower())
    leaves = [airdrop_leaf(addr, amt) for addr, amt in items]
    levels = build_levels(leaves)
    root = levels[-1][0] if levels else b"\x00" * 32

    proofs: dict[str, list[bytes]] = {}
    for i, (addr, _) in enumerate(items):
        proofs[addr] = proof_for(levels, i)
    return root, proofs


def build_dual_airdrop_tree(
    allocations: dict[str, tuple[int, int]],
) -> tuple[bytes, dict[str, list[bytes]]]:
    """Given {address: (ardi_amount, awp_amount)}, return (root, {address: proof}).

    Leaves are sorted by address — deterministic for any given input.
    Proof verifies against keccak256(abi.encodePacked(addr, ardi, awp)).
    """
    items = sorted(allocations.items(), key=lambda kv: kv[0].lower())
    leaves = [dual_airdrop_leaf(addr, ardi, awp) for addr, (ardi, awp) in items]
    levels = build_levels(leaves)
    root = levels[-1][0] if levels else b"\x00" * 32

    proofs: dict[str, list[bytes]] = {}
    for i, (addr, _) in enumerate(items):
        proofs[addr] = proof_for(levels, i)
    return root, proofs
