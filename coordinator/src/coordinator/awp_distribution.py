"""AWP receipt distribution math (off-chain side of the AWP-aligned Manager).

The AWP protocol pushes a daily AWP balance to the ArdiMintController. The
Coordinator splits that balance into:

  - operator ops cut  (`owner_ops_bps` / 10_000, 10% by default)
  - holder pool       (the remainder, 90% by default)

The split itself happens entirely on-chain via `settleDay(..., awpToHolders,
awpOwnerCut)` — this module just computes the values the Coordinator passes.

The contract enforces that `awpToHolders + awpOwnerCut <= AWP balance held by
the controller minus already-reserved buckets`, so we deliberately avoid
floating-point: we split using integer division and assign rounding remainder
to the holder bucket (since the hard cap is a *Timelock-set* basis-point cap,
giving holders the dust is always safe).
"""
from __future__ import annotations

from dataclasses import dataclass


# Hard upper bound on owner_ops_bps. MUST match
# `ArdiMintController.MAX_OWNER_OPS_BPS` in Solidity. The contract enforces
# this; we replicate it here so a misconfigured Coordinator never sends a tx
# that would just revert.
MAX_OWNER_OPS_BPS = 2000  # 20%


@dataclass
class AwpSplit:
    awp_received: int       # AWP newly available to allocate this day
    awp_to_holders: int      # 90% by default
    awp_owner_cut: int       # 10% by default
    owner_ops_bps: int        # bps applied for this split


def split_awp(awp_received: int, owner_ops_bps: int) -> AwpSplit:
    """Split today's AWP receipt into operator-ops + holder pool.

    `awp_received` is the *delta* — i.e. the AWP just pushed by the AWP
    protocol since the previous settlement. Caller is responsible for
    computing this correctly (see `awp_received_today` below).

    Owner cut uses integer division; the remainder accrues to holders.
    """
    if awp_received < 0:
        raise ValueError("awp_received must be non-negative")
    if owner_ops_bps < 0 or owner_ops_bps > MAX_OWNER_OPS_BPS:
        raise ValueError(
            f"owner_ops_bps must be in [0, {MAX_OWNER_OPS_BPS}], got {owner_ops_bps}"
        )

    owner_cut = (awp_received * owner_ops_bps) // 10_000
    holders = awp_received - owner_cut
    return AwpSplit(
        awp_received=awp_received,
        awp_to_holders=holders,
        awp_owner_cut=owner_cut,
        owner_ops_bps=owner_ops_bps,
    )


def awp_received_today(
    awp_balance_now: int,
    owner_awp_reserve: int,
    awp_reserved_for_claims: int,
) -> int:
    """Compute today's allocatable AWP: total balance minus already-committed buckets.

    Mirrors the on-chain accounting in ArdiMintController:
      receivable = balanceOf(controller) - ownerAwpReserve - awpReservedForClaims

    Anything in those two reserves is already earmarked for a previous day's
    settlement, so it must NOT be re-distributed.
    """
    receivable = awp_balance_now - owner_awp_reserve - awp_reserved_for_claims
    if receivable < 0:
        # Indicates either a bug in our accounting or a token rebase event —
        # in either case, refuse to settle until reconciled.
        raise RuntimeError(
            "AWP accounting drift: balance < reserves "
            f"(balance={awp_balance_now}, ownerReserve={owner_awp_reserve}, "
            f"holderReserve={awp_reserved_for_claims})"
        )
    return receivable


def distribute_to_holders(
    awp_to_holders: int,
    holder_powers: dict[str, int],
) -> dict[str, int]:
    """Power-weighted AWP distribution. Same shape + rounding policy as the
    $aArdi distribution in `settlement.compute_day` so a single Merkle leaf
    with both amounts is consistent across both streams.
    """
    if not holder_powers or awp_to_holders == 0:
        return {}
    total_power = sum(holder_powers.values())
    if total_power == 0:
        return {}

    addresses = sorted(holder_powers.keys(), key=lambda a: a.lower())
    out: dict[str, int] = {}
    cumulative = 0
    for i, addr in enumerate(addresses):
        if i == len(addresses) - 1:
            amt = awp_to_holders - cumulative
        else:
            amt = (awp_to_holders * holder_powers[addr]) // total_power
            cumulative += amt
        if amt > 0:
            out[addr] = amt
    return out
