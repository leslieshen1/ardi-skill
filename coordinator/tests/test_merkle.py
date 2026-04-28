"""Test Merkle tree builder matches OpenZeppelin's MerkleProof verifier."""
from coordinator.merkle import (
    airdrop_leaf,
    build_airdrop_tree,
    build_dual_airdrop_tree,
    build_levels,
    dual_airdrop_leaf,
    hash_pair,
    proof_for,
)


def test_pair_hashing_sorted():
    a = b"\x01" + b"\x00" * 31
    b = b"\x02" + b"\x00" * 31
    assert hash_pair(a, b) == hash_pair(b, a)  # sorted


def test_simple_tree():
    leaves = [b"\x01" + b"\x00" * 31, b"\x02" + b"\x00" * 31]
    levels = build_levels(leaves)
    assert len(levels) == 2
    assert levels[1][0] == hash_pair(leaves[0], leaves[1])


def test_proof_verifies():
    # 4 leaves
    leaves = [bytes([i + 1]) + b"\x00" * 31 for i in range(4)]
    levels = build_levels(leaves)
    root = levels[-1][0]

    # Verify proof for leaf 0
    p = proof_for(levels, 0)
    # Manually compute root from leaf 0 + proof
    h = leaves[0]
    for sib in p:
        h = hash_pair(h, sib)
    assert h == root


def test_airdrop_tree():
    allocs = {
        "0x1111111111111111111111111111111111111111": 100,
        "0x2222222222222222222222222222222222222222": 200,
        "0x3333333333333333333333333333333333333333": 300,
    }
    root, proofs = build_airdrop_tree(allocs)
    assert len(root) == 32
    assert all(addr in proofs for addr in allocs)

    # For each holder, proof should reconstruct root
    for addr, amt in allocs.items():
        leaf = airdrop_leaf(addr, amt)
        h = leaf
        for sib in proofs[addr]:
            h = hash_pair(h, sib)
        assert h == root


def test_dual_airdrop_leaf_format():
    """Leaf bytes must equal keccak256(addr || ardi(uint256) || awp(uint256))."""
    from eth_utils import keccak

    addr = "0x1111111111111111111111111111111111111111"
    ardi = 12345
    awp = 67890
    expected = keccak(
        bytes.fromhex(addr[2:])
        + ardi.to_bytes(32, "big")
        + awp.to_bytes(32, "big")
    )
    assert dual_airdrop_leaf(addr, ardi, awp) == expected


def test_dual_airdrop_tree_proofs_verify():
    allocs = {
        "0x1111111111111111111111111111111111111111": (100, 5),
        "0x2222222222222222222222222222222222222222": (200, 10),
        "0x3333333333333333333333333333333333333333": (300, 15),
    }
    root, proofs = build_dual_airdrop_tree(allocs)
    assert len(root) == 32

    for addr, (ardi, awp) in allocs.items():
        leaf = dual_airdrop_leaf(addr, ardi, awp)
        h = leaf
        for sib in proofs[addr]:
            h = hash_pair(h, sib)
        assert h == root


def test_dual_airdrop_tree_different_from_single():
    """Single-token and dual-token trees must produce different roots even
    when awp=0 for everyone — leaf encoding must include both fields."""
    single = {
        "0x1111111111111111111111111111111111111111": 100,
    }
    dual = {
        "0x1111111111111111111111111111111111111111": (100, 0),
    }
    s_root, _ = build_airdrop_tree(single)
    d_root, _ = build_dual_airdrop_tree(dual)
    assert s_root != d_root
