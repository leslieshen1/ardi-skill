"""Test Coordinator fuse signer produces signatures matching the on-chain digest format.

The on-chain ArdiNFT.fuse verifier:
  1. Builds digest = keccak256(abi.encodePacked("ARDI_FUSE_V2", chainId, contract, holder, ...args))
  2. Wraps via MessageHashUtils.toEthSignedMessageHash:
     ethSigned = keccak256("\\x19Ethereum Signed Message:\\n32" || digest)
  3. ECDSA.recover(ethSigned, signature) must equal coordinator address

Note: under the on-chain commit-reveal architecture, the inscribe path is fully
on-chain and no longer involves Coordinator signing at all. Only `fuse` retains
a Coordinator signature because LLM oracle output cannot be reconstructed on-chain.
"""
from coordinator.signer import Signer, fuse_digest
from eth_account import Account
from eth_account.messages import encode_defunct


def test_fuse_digest_deterministic():
    d1 = fuse_digest(
        chain_id=8453,
        contract="0x1111111111111111111111111111111111111111",
        holder="0x2222222222222222222222222222222222222222",
        token_a=10,
        token_b=11,
        new_word="steam",
        new_power=280,
        new_lang_id=0,
        success=True,
        nonce=0,
    )
    d2 = fuse_digest(
        chain_id=8453,
        contract="0x1111111111111111111111111111111111111111",
        holder="0x2222222222222222222222222222222222222222",
        token_a=10,
        token_b=11,
        new_word="steam",
        new_power=280,
        new_lang_id=0,
        success=True,
        nonce=0,
    )
    assert d1 == d2
    assert len(d1) == 32

    # Tampering changes digest
    d3 = fuse_digest(
        chain_id=8453,
        contract="0x1111111111111111111111111111111111111111",
        holder="0x2222222222222222222222222222222222222222",
        token_a=10,
        token_b=11,
        new_word="steam",
        new_power=999,  # changed
        new_lang_id=0,
        success=True,
        nonce=0,
    )
    assert d1 != d3


def test_signer_recovery():
    pk = "0x" + "11" * 32
    expected_addr = Account.from_key(pk).address

    signer = Signer(pk)
    sig = signer.sign_fuse(
        chain_id=8453,
        contract="0x1111111111111111111111111111111111111111",
        holder="0x2222222222222222222222222222222222222222",
        token_a=10,
        token_b=11,
        new_word="steam",
        new_power=280,
        new_lang_id=0,
        success=True,
        nonce=0,
    )
    assert len(sig) == 65

    digest = fuse_digest(
        8453,
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
        10,
        11,
        "steam",
        280,
        0,
        True,
        0,
    )
    msg = encode_defunct(primitive=digest)
    recovered = Account.recover_message(msg, signature=sig)
    assert recovered == expected_addr


def test_fuse_digest_changes_with_holder():
    """V2: changing holder must change the digest (replay-resistance proof)."""
    common = dict(
        chain_id=8453,
        contract="0x1111111111111111111111111111111111111111",
        token_a=10,
        token_b=11,
        new_word="steam",
        new_power=280,
        new_lang_id=0,
        success=True,
        nonce=0,
    )
    d_alice = fuse_digest(holder="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", **common)
    d_bob = fuse_digest(holder="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", **common)
    assert d_alice != d_bob
