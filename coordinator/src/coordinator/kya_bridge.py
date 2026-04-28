"""KYA bridge — read KYA attestations and propagate sybil flags to BondEscrow.

Exposes:
  - is_verified(addr) — checks KYA contract for valid attestation
  - poll_sybil_flags() — periodic scan of registered miners; for any flagged ones,
                         calls BondEscrow.slashOnSybil
"""
from __future__ import annotations

import logging
from typing import Iterable

from .config import Config

log = logging.getLogger("ardi.kya")


# Minimal ABI for the methods we call
KYA_ABI = [
    {
        "inputs": [{"name": "agent", "type": "address"}],
        "name": "isVerified",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "agent", "type": "address"}],
        "name": "isSybilFlagged",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

BOND_ABI = [
    {
        "inputs": [{"name": "agent", "type": "address"}, {"name": "bps", "type": "uint16"}],
        "name": "slashOnSybil",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


class KYABridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._w3 = None
        self._kya = None
        self._bond = None

    def _w3_lazy(self):
        if self._w3 is None:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self.cfg.chain.rpc_url))
            self._kya = self._w3.eth.contract(address=self.cfg.contracts.kya, abi=KYA_ABI)
            self._bond = self._w3.eth.contract(
                address=self.cfg.contracts.bond_escrow, abi=BOND_ABI
            )
        return self._w3

    def is_verified(self, agent: str) -> bool:
        self._w3_lazy()
        try:
            return bool(self._kya.functions.isVerified(agent).call())
        except Exception as e:
            log.error(f"KYA isVerified failed for {agent}: {e}")
            return False

    def is_sybil_flagged(self, agent: str) -> bool:
        self._w3_lazy()
        try:
            return bool(self._kya.functions.isSybilFlagged(agent).call())
        except Exception as e:
            log.error(f"KYA isSybilFlagged failed for {agent}: {e}")
            return False

    def slash(self, agent: str, bps: int = 5000) -> str | None:
        """Submit slashOnSybil tx. Returns tx hash hex."""
        from eth_account import Account

        self._w3_lazy()
        if not self.cfg.coordinator.sender_pk:
            log.error("Coordinator sender_pk not set — cannot slash")
            return None
        sender = Account.from_key(self.cfg.coordinator.sender_pk)
        nonce = self._w3.eth.get_transaction_count(sender.address)
        tx = self._bond.functions.slashOnSybil(agent, bps).build_transaction(
            {
                "from": sender.address,
                "nonce": nonce,
                "chainId": self.cfg.chain.chain_id,
                "gas": 300000,
                "gasPrice": self._w3.eth.gas_price,
            }
        )
        signed = sender.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info(f"slash submitted for {agent} bps={bps} tx={tx_hash.hex()}")
        return tx_hash.hex()

    def poll_sybil_flags(self, miners: Iterable[str], default_bps: int = 5000) -> list[dict]:
        """For each miner address, check sybil flag; slash if flagged."""
        results = []
        for agent in miners:
            if self.is_sybil_flagged(agent):
                tx = self.slash(agent, default_bps)
                results.append({"agent": agent, "slashed": True, "tx": tx})
            else:
                results.append({"agent": agent, "slashed": False})
        return results
