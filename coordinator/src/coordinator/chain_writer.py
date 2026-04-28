"""ChainWriter — wraps web3 calls to ArdiEpochDraw + ArdiNFT for the
on-chain commit-reveal flow.

Centralizes all on-chain side effects so:
  - tests can swap in a Mock (None passes through silently)
  - real deployments share one signed-tx pipeline (gas + nonce + retry)
  - audit can review the entire chain-write surface in one file
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ardi.chain_writer")


# Minimal ABI fragments for the calls we make
EPOCH_DRAW_ABI = [
    {
        "name": "openEpoch",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "epochId", "type": "uint256"},
            {"name": "commitWindow", "type": "uint64"},
            {"name": "revealWindow", "type": "uint64"},
        ],
        "outputs": [],
    },
    # v1.0: hash-only publish. The Coordinator submits keccak256(word) instead
    # of the plaintext word; the vault Merkle leaf is over (wordId, wordHash,
    # power, lang). Plaintext is supplied later by the winner at inscribe.
    {
        "name": "publishAnswer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "epochId", "type": "uint256"},
            {"name": "wordId", "type": "uint256"},
            {"name": "wordHash", "type": "bytes32"},
            {"name": "power", "type": "uint16"},
            {"name": "languageId", "type": "uint8"},
            {"name": "vaultProof", "type": "bytes32[]"},
        ],
        "outputs": [],
    },
    {
        "name": "requestDraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "epochId", "type": "uint256"},
            {"name": "wordId", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "winners",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "", "type": "uint256"},
            {"name": "", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "correctCount",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "epochId", "type": "uint256"},
            {"name": "wordId", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


class ChainWriter:
    """Owns the Coordinator's signing key + nonce; sends txs to ArdiEpochDraw."""

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        epoch_draw_address: str,
        chain_id: int,
        gas_limit_default: int = 500_000,
    ):
        from web3 import Web3
        from eth_account import Account

        self._w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._account = Account.from_key(private_key)
        self._chain_id = chain_id
        self._gas_default = gas_limit_default
        self._draw = self._w3.eth.contract(
            address=Web3.to_checksum_address(epoch_draw_address), abi=EPOCH_DRAW_ABI
        )

    @property
    def address(self) -> str:
        return self._account.address

    def _send(self, tx: dict[str, Any]) -> str:
        # Use 'pending' tag so consecutive sends don't reuse the confirmed
        # nonce while a previous tx is still in the mempool. Plus a local
        # cache that monotonically advances — public RPCs (Base Sepolia)
        # sometimes return stale pending counts right after a send.
        chain_nonce = self._w3.eth.get_transaction_count(self._account.address, "pending")
        cached = getattr(self, "_last_nonce", -1)
        nonce = max(chain_nonce, cached + 1)
        self._last_nonce = nonce
        tx.update({
            "from": self._account.address,
            "nonce": nonce,
            "chainId": self._chain_id,
        })
        if "gas" not in tx:
            tx["gas"] = self._gas_default
        if "gasPrice" not in tx and "maxFeePerGas" not in tx:
            tx["gasPrice"] = self._w3.eth.gas_price
        signed = self._account.sign_transaction(tx)
        h = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self._w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"tx reverted: {h.hex()}")
        return h.hex()

    # --------------------- ArdiEpochDraw calls ---------------------

    def open_epoch(self, epoch_id: int, commit_window: int, reveal_window: int) -> str:
        # Pass explicit gas + from so build_transaction doesn't trigger
        # estimate_gas with a default zero-address from (which would fail
        # the contract's `if (msg.sender != coordinator) revert NotCoordinator`
        # check at eth_call time on strict public RPCs like Base Sepolia).
        tx = self._draw.functions.openEpoch(
            epoch_id, commit_window, reveal_window
        ).build_transaction({"gas": 150_000, "from": self._account.address})
        return self._send(tx)

    def publish_answer(
        self,
        epoch_id: int,
        word_id: int,
        word: str,
        power: int,
        language_id: int,
        merkle_proof: list[bytes],
    ) -> str:
        """v1.0: publishAnswer submits keccak(word), not plaintext. We accept
        `word` here for caller convenience (callers already have plaintext at
        publish time) and hash it inline so the chain layer never sees it."""
        from eth_utils import keccak as _keccak
        word_hash = _keccak(word.encode("utf-8"))
        tx = self._draw.functions.publishAnswer(
            epoch_id, word_id, word_hash, power, language_id, merkle_proof
        ).build_transaction({"gas": 350_000, "from": self._account.address})
        return self._send(tx)

    def request_draw(self, epoch_id: int, word_id: int) -> str:
        tx = self._draw.functions.requestDraw(
            epoch_id, word_id
        ).build_transaction({"gas": 300_000, "from": self._account.address})
        return self._send(tx)

    # --------------------- Read methods ---------------------

    def winner_of(self, epoch_id: int, word_id: int) -> str:
        return self._draw.functions.winners(epoch_id, word_id).call()

    def correct_count(self, epoch_id: int, word_id: int) -> int:
        return int(self._draw.functions.correctCount(epoch_id, word_id).call())
