"""Coordinator signing module — produces ECDSA signatures matching the
on-chain verifier in ArdiNFT.fuse.

Under the on-chain commit-reveal architecture, the inscribe path no longer
requires any Coordinator signature — winners are verified on-chain via
ArdiEpochDraw.winners(). The only signing surface that remains is `fuse`,
because LLM oracle output (newWord, newPower, newLangId) cannot be
reconstructed on-chain.

Hash format is bound to chainId + contract address + the V2 tag prefix to
prevent cross-chain / cross-contract / cross-version replay, plus the
holder address to prevent leaked-signature redemption by a different
account that later acquires both tokens.
"""
from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak
from web3 import Web3


def fuse_digest(
    chain_id: int,
    contract: str,
    holder: str,
    token_a: int,
    token_b: int,
    new_word: str,
    new_power: int,
    new_lang_id: int,
    success: bool,
    nonce: int,
) -> bytes:
    """V2: binds `holder` (msg.sender of the on-chain fuse call) into the digest
    so a leaked signature cannot be redeemed by a different address that later
    acquires both tokens (e.g. via OTC) before the original holder submits."""
    payload = (
        b"ARDI_FUSE_V2"
        + int(chain_id).to_bytes(32, "big")
        + bytes.fromhex(Web3.to_checksum_address(contract)[2:])
        + bytes.fromhex(Web3.to_checksum_address(holder)[2:])
        + int(token_a).to_bytes(32, "big")
        + int(token_b).to_bytes(32, "big")
        + new_word.encode("utf-8")
        + int(new_power).to_bytes(2, "big")
        + int(new_lang_id).to_bytes(1, "big")
        + (b"\x01" if success else b"\x00")
        + int(nonce).to_bytes(32, "big")
    )
    return keccak(payload)


class Signer:
    """ECDSA signer using EIP-191 personal_sign over the digest.

    The on-chain verifier wraps the raw keccak hash in
        keccak256("\\x19Ethereum Signed Message:\\n32" + digest)
    via OpenZeppelin's MessageHashUtils.toEthSignedMessageHash. We use
    eth_account's encode_defunct to mirror that exactly.

    Two construction paths:
      Signer(private_key="0x...")            — backwards compatible env-var path
      Signer(key_provider=AwsKmsKeyProvider(...))  — production HSM/KMS path

    The `key_provider` is the abstraction boundary; see
    `coordinator/key_provider.py` for the available providers.
    """

    def __init__(self, private_key: str | None = None, key_provider=None):
        if key_provider is not None:
            self._provider = key_provider
        else:
            if not private_key:
                raise ValueError("Coordinator private key not configured")
            from .key_provider import EnvVarKeyProvider
            self._provider = EnvVarKeyProvider(private_key)

    @property
    def address(self) -> str:
        return self._provider.address

    def sign_digest(self, digest: bytes) -> bytes:
        """Returns 65-byte r||s||v signature."""
        return self._provider.sign_eth_message(digest)

    def sign_fuse(
        self,
        chain_id: int,
        contract: str,
        holder: str,
        token_a: int,
        token_b: int,
        new_word: str,
        new_power: int,
        new_lang_id: int,
        success: bool,
        nonce: int,
    ) -> bytes:
        digest = fuse_digest(
            chain_id, contract, holder, token_a, token_b, new_word, new_power, new_lang_id, success, nonce
        )
        return self.sign_digest(digest)
