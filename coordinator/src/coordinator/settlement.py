"""Daily settlement worker (AWP-aligned dual-token Merkle).

Each UTC day at the configured hour:
  1. Snapshot active Ardinal holdings (from chain via indexer)
  2. Compute per-holder Power weight: sum(power(token)) for tokens held
  3. Compute daily $aArdi emission per the on-chain ArdiMintController formula
  4. Read AWP held by the controller, subtract reserves → today's AWP receipt
  5. Split AWP: `owner_ops_bps` to ops reserve (10% default, Timelock-adjustable),
     remainder to holders by power weight
  6. Build Merkle tree of {address: (ardi_amount, awp_amount)}
  7. Submit (day, root, ardiTotal, awpToHolders, awpOwnerCut) to
     ArdiMintController.settleDay
  8. Persist tree (full leaves + proofs) so /v1/airdrop/proof can serve queries

A holder's single claim() call distributes BOTH $aArdi and AWP to them.
"""
from __future__ import annotations

import json
import logging
import time

from .awp_distribution import distribute_to_holders, split_awp
from .config import Config
from .db import DB
from .merkle import build_dual_airdrop_tree

log = logging.getLogger("ardi.settlement")


# Two-phase emission constants — must match ArdiMintController.sol exactly
PHASE1_DAYS = 14
PHASE2_DAYS = 166
PHASE1_DAY1 = 954_277_300 * 10**18
PHASE1_NUM, PHASE1_DEN = 8706, 10000
PHASE2_DAY1 = 63_375_500 * 10**18
PHASE2_NUM, PHASE2_DEN = 9772, 10000


def daily_emission(day: int) -> int:
    if day <= 0 or day > PHASE1_DAYS + PHASE2_DAYS:
        return 0
    if day <= PHASE1_DAYS:
        v = PHASE1_DAY1
        for _ in range(day - 1):
            v = (v * PHASE1_NUM) // PHASE1_DEN
        return v
    v = PHASE2_DAY1
    phase2_idx = day - PHASE1_DAYS - 1
    for _ in range(phase2_idx):
        v = (v * PHASE2_NUM) // PHASE2_DEN
    return v


def _power_distribute(total: int, holder_powers: dict[str, int]) -> dict[str, int]:
    """Power-weighted integer distribution. Last holder absorbs rounding dust."""
    if total == 0 or not holder_powers:
        return {}
    total_power = sum(holder_powers.values())
    if total_power == 0:
        return {}
    addresses = sorted(holder_powers.keys(), key=lambda a: a.lower())
    out: dict[str, int] = {}
    cumulative = 0
    for i, addr in enumerate(addresses):
        if i == len(addresses) - 1:
            amt = total - cumulative
        else:
            amt = (total * holder_powers[addr]) // total_power
            cumulative += amt
        if amt > 0:
            out[addr] = amt
    return out


class Settlement:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db

    def compute_day(
        self,
        day: int,
        holder_powers: dict[str, int],
        *,
        awp_received: int = 0,
        owner_ops_bps: int | None = None,
    ) -> dict:
        """Compute the day's dual-token settlement.

        Args:
            day:           1-indexed day number (matches ArdiMintController._currentDay).
            holder_powers: {checksum_address: total_power} snapshot.
            awp_received:  AWP newly receivable today (after subtracting on-chain reserves).
                           Caller computes this via awp_distribution.awp_received_today.
            owner_ops_bps: Override the on-chain bps. If None, falls back to cfg.

        Returns a dict with:
            ardi_total       : $aArdi distributed via this Merkle (claim-time mint)
            awp_to_holders    : AWP distributed to holders via this Merkle
            awp_owner_cut     : AWP added to ownerAwpReserve on settleDay
            leaves            : {address: (ardi_amount, awp_amount)}
        """
        # $aArdi emission — 100% to holders by power
        emission = daily_emission(day)
        ardi_per_holder = _power_distribute(emission, holder_powers)
        ardi_total_distributed = sum(ardi_per_holder.values())

        # AWP split — 10/90 by default
        if owner_ops_bps is None:
            owner_ops_bps = getattr(self.cfg.settlement, "owner_ops_bps", 1000)
        split = split_awp(awp_received, owner_ops_bps)
        awp_per_holder = distribute_to_holders(split.awp_to_holders, holder_powers)
        # If there are no holders to receive the AWP, redirect the holder
        # bucket to the ops reserve so the pushed AWP doesn't get stranded.
        if not awp_per_holder and split.awp_to_holders > 0:
            adjusted_owner_cut = split.awp_owner_cut + split.awp_to_holders
            adjusted_to_holders = 0
        else:
            adjusted_owner_cut = split.awp_owner_cut
            adjusted_to_holders = sum(awp_per_holder.values())

        # Merge per-holder ardi + awp into a single dual-token leaf table.
        leaves: dict[str, tuple[int, int]] = {}
        all_addrs = set(ardi_per_holder) | set(awp_per_holder)
        for addr in all_addrs:
            leaves[addr] = (ardi_per_holder.get(addr, 0), awp_per_holder.get(addr, 0))

        return {
            "day": day,
            "emission": emission,
            "ardi_total": ardi_total_distributed,
            "awp_received": awp_received,
            "awp_to_holders": adjusted_to_holders,
            "awp_owner_cut": adjusted_owner_cut,
            "owner_ops_bps": owner_ops_bps,
            "leaves": leaves,
        }

    def store_settlement(self, day: int, payload: dict) -> dict:
        """Build dual-token Merkle tree, persist to DB, return root + tx-ready params."""
        leaves = payload["leaves"]
        if not leaves:
            root = b"\x00" * 32
        else:
            root, _proofs = build_dual_airdrop_tree(leaves)

        # Serialize leaves as {addr: [ardi, awp]} (JSON-friendly, ints as strings to
        # avoid precision loss across language boundaries).
        leaves_json = {addr: [str(a), str(w)] for addr, (a, w) in leaves.items()}

        with self.db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO daily_settlement "
                "(day, root, ardi_total, awp_to_holders, awp_owner_cut, leaves_json, submitted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    day,
                    "0x" + root.hex(),
                    str(payload["ardi_total"]),
                    str(payload["awp_to_holders"]),
                    str(payload["awp_owner_cut"]),
                    json.dumps(leaves_json),
                    int(time.time()),
                ),
            )

        return {
            "day": day,
            "root_hex": "0x" + root.hex(),
            "ardi_total": payload["ardi_total"],
            "awp_to_holders": payload["awp_to_holders"],
            "awp_owner_cut": payload["awp_owner_cut"],
            "tx_args": {
                "day": day,
                "root": "0x" + root.hex(),
                "ardiTotal": str(payload["ardi_total"]),
                "awpToHolders": str(payload["awp_to_holders"]),
                "awpOwnerCut": str(payload["awp_owner_cut"]),
            },
        }
