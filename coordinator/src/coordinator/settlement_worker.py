"""Settlement worker — daily Merkle root submission to ArdiMintController.

AWP-aligned: each tick reads the controller's AWP balance + reserves, computes
the day's split (10/90 default), and submits a single Merkle root that pays
both $aArdi and AWP to holders in one claim.

Runs as an async loop alongside the epoch engine. Each tick:
  1. Compute the current "settlement day" (days since GENESIS_TS, integer)
  2. If we've already settled this day, skip
  3. Snapshot holder_powers from the Indexer
  4. Read controller AWP balance + reserves → today's allocatable AWP
  5. Run Settlement.compute_day(...) → dual-token Merkle leaves
  6. Persist via Settlement.store_settlement(...)
  7. Submit ArdiMintController.settleDay(day, root, ardiTotal, awpToHolders, awpOwnerCut)
"""
from __future__ import annotations

import asyncio
import logging
import time

from eth_account import Account

from .awp_distribution import awp_received_today
from .config import Config
from .db import DB
from .indexer import Indexer
from .metrics import metrics as _metrics
from .settlement import Settlement, daily_emission

log = logging.getLogger("ardi.settlement_worker")


# Minimal ABI for ArdiMintController. Stays in lockstep with the Solidity
# contract — when the contract changes, update both at once.
MINT_CONTROLLER_ABI = [
    {
        "name": "settleDay",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "day", "type": "uint256"},
            {"name": "root", "type": "bytes32"},
            {"name": "ardiTotal", "type": "uint256"},
            {"name": "awpToHolders", "type": "uint256"},
            {"name": "awpOwnerCut", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "lastSettledDay",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "ownerAwpReserve",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "awpReservedForClaims",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "ownerOpsBps",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint16"}],
    },
    {
        "name": "AWP",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]

# Minimal ERC20 ABI for AWP balanceOf reads.
ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


class SettlementWorker:
    """Daily settlement worker. Polls every `tick_interval` seconds and if a
    new settlement day boundary has been crossed, computes + submits."""

    def __init__(
        self,
        cfg: Config,
        db: DB,
        indexer: Indexer,
        tick_interval: int = 300,  # poll every 5 min by default
    ):
        self.cfg = cfg
        self.db = db
        self.indexer = indexer
        self.tick_interval = tick_interval
        self.settlement = Settlement(cfg, db)
        self._stopped = False

        # Lazy chain client setup
        self._w3 = None
        self._mc = None
        self._awp = None
        self._sender = None

    def _ensure_chain(self):
        if self._w3 is None:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self.cfg.chain.rpc_url))
            self._mc = self._w3.eth.contract(
                address=self.cfg.contracts.mint_controller, abi=MINT_CONTROLLER_ABI
            )
            # Resolve AWP address from the controller (single source of truth).
            try:
                awp_addr = self._mc.functions.AWP().call()
                self._awp = self._w3.eth.contract(address=awp_addr, abi=ERC20_ABI)
            except Exception as e:
                log.warning(f"failed to resolve AWP address from controller: {e}")
                self._awp = None
            if self.cfg.coordinator.sender_pk:
                self._sender = Account.from_key(self.cfg.coordinator.sender_pk)
            else:
                log.warning("no sender_pk configured — settlement tx submission disabled")

    def _last_settled_on_chain(self) -> int:
        try:
            return int(self._mc.functions.lastSettledDay().call())
        except Exception as e:
            log.warning(f"failed to read lastSettledDay: {e}")
            return 0

    def _current_day(self) -> int:
        # Compute from local time + GENESIS_TS in cfg. The on-chain function
        # was removed — _currentDay is internal — so we mirror the formula.
        if self.cfg.mining.genesis_ts == 0:
            return 0
        return ((int(time.time()) - self.cfg.mining.genesis_ts) // 86400) + 1

    def _read_awp_state(self) -> tuple[int, int, int, int]:
        """Return (awp_balance_now, owner_reserve, holder_reserve, on_chain_bps)."""
        if self._awp is None:
            return 0, 0, 0, getattr(self.cfg.settlement, "owner_ops_bps", 1000)
        try:
            balance = int(self._awp.functions.balanceOf(self._mc.address).call())
        except Exception as e:
            log.warning(f"failed to read AWP balance: {e}")
            balance = 0
        try:
            owner_reserve = int(self._mc.functions.ownerAwpReserve().call())
        except Exception:
            owner_reserve = 0
        try:
            holder_reserve = int(self._mc.functions.awpReservedForClaims().call())
        except Exception:
            holder_reserve = 0
        try:
            bps = int(self._mc.functions.ownerOpsBps().call())
        except Exception:
            bps = getattr(self.cfg.settlement, "owner_ops_bps", 1000)
        return balance, owner_reserve, holder_reserve, bps

    async def settle_day(self, day: int) -> dict | None:
        """Compute + submit settlement for `day`. Returns submission summary or None."""
        self._ensure_chain()

        # Skip if already settled (idempotent)
        with self.db.conn() as c:
            row = c.execute(
                "SELECT day FROM daily_settlement WHERE day = ?", (day,)
            ).fetchone()
            if row:
                log.debug(f"day {day} already settled locally")
                return None

        emission = daily_emission(day)
        # Even if $aArdi emission is 0 (post-day-180), AWP may still be flowing,
        # so we still proceed — settlement is gated on emission *or* AWP being
        # available, not just emission.

        # Snapshot from indexer
        holder_powers = self.indexer.holder_powers()
        log.info(
            f"day {day}: snapshot {len(holder_powers)} holders "
            f"with total power {sum(holder_powers.values())}"
        )

        # Read AWP state from chain to determine today's split
        balance, owner_res, holder_res, on_chain_bps = self._read_awp_state()
        try:
            awp_recv = awp_received_today(balance, owner_res, holder_res)
        except RuntimeError as e:
            log.error(f"day {day}: AWP accounting drift, skipping settlement: {e}")
            return None
        log.info(
            f"day {day}: AWP balance={balance}, ownerReserve={owner_res}, "
            f"holderReserve={holder_res} → receivable={awp_recv} (bps={on_chain_bps})"
        )

        if emission == 0 and awp_recv == 0:
            log.info(f"day {day}: nothing to settle (emission=0, awp=0)")
            return None

        # Compute (use on-chain bps as source of truth so Coordinator+contract
        # never disagree on the split)
        payload = self.settlement.compute_day(
            day,
            holder_powers,
            awp_received=awp_recv,
            owner_ops_bps=on_chain_bps,
        )
        record = self.settlement.store_settlement(day, payload)

        # Submit on-chain if sender configured
        if self._sender is None:
            log.warning(f"day {day}: settlement computed but tx submission skipped (no sender)")
            return record

        try:
            tx = self._mc.functions.settleDay(
                day,
                bytes.fromhex(record["root_hex"][2:]),
                int(record["ardi_total"]),
                int(record["awp_to_holders"]),
                int(record["awp_owner_cut"]),
            ).build_transaction(
                {
                    "from": self._sender.address,
                    "nonce": self._w3.eth.get_transaction_count(self._sender.address),
                    "chainId": self.cfg.chain.chain_id,
                    "gas": 600000,
                    "gasPrice": self._w3.eth.gas_price,
                }
            )
            signed = self._sender.sign_transaction(tx)
            h = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(h, timeout=120)
            tx_hash = h.hex()
            log.info(f"day {day} settled on-chain, tx={tx_hash} status={receipt.status}")

            # Persist tx hash
            with self.db.conn() as c:
                c.execute(
                    "UPDATE daily_settlement SET tx_hash = ? WHERE day = ?",
                    (tx_hash, day),
                )
            record["tx_hash"] = tx_hash
            _metrics.gauge("ardi_last_settlement_submit_ts", float(int(time.time())))
            _metrics.inc("ardi_settlements_submitted_total")
            return record
        except Exception as e:
            log.error(f"day {day} on-chain settlement failed: {e}", exc_info=True)
            return record

    async def run_loop(self):
        log.info(f"settlement worker starting (tick={self.tick_interval}s)")
        while not self._stopped:
            try:
                self._ensure_chain()
                last_settled = self._last_settled_on_chain()
                current = self._current_day()
                # Settle every day strictly after last_settled, up to current-1
                # (current day itself is not yet complete, we settle yesterday)
                target_day = current - 1 if current > 0 else 0
                if target_day > last_settled:
                    for day in range(last_settled + 1, target_day + 1):
                        await self.settle_day(day)
            except Exception as e:
                log.error(f"settlement loop tick error: {e}", exc_info=True)
            await asyncio.sleep(self.tick_interval)

    def stop(self):
        self._stopped = True
