"""Test settlement allocations and emission curve match Solidity."""
import os
import tempfile
from dataclasses import dataclass

from coordinator.awp_distribution import (
    awp_received_today,
    distribute_to_holders,
    split_awp,
)
from coordinator.db import DB
from coordinator.settlement import Settlement, daily_emission


# --- Emission curve sanity (must match ArdiMintController.sol exactly) ---


def test_emission_phase1_day1():
    assert daily_emission(1) == 954_277_300 * 10**18


def test_emission_phase1_day14():
    v = daily_emission(14)
    assert 130_000_000 * 10**18 < v < 160_000_000 * 10**18


def test_emission_phase2_day15():
    assert daily_emission(15) == 63_375_500 * 10**18


def test_emission_after_180():
    assert daily_emission(181) == 0
    assert daily_emission(0) == 0
    assert daily_emission(-1) == 0


def test_total_approx_9b():
    total = sum(daily_emission(d) for d in range(1, 181))
    assert 8_500_000_000 * 10**18 < total < 9_500_000_000 * 10**18


# --- AWP split logic ---


def test_split_awp_default_10_90():
    s = split_awp(1_000_000, owner_ops_bps=1000)
    assert s.awp_owner_cut == 100_000
    assert s.awp_to_holders == 900_000
    assert s.awp_received == 1_000_000


def test_split_awp_zero_bps():
    s = split_awp(1_000_000, owner_ops_bps=0)
    assert s.awp_owner_cut == 0
    assert s.awp_to_holders == 1_000_000


def test_split_awp_capped():
    import pytest
    with pytest.raises(ValueError):
        split_awp(1_000_000, owner_ops_bps=2001)  # > MAX_OWNER_OPS_BPS


def test_split_awp_rounding_to_holders():
    # 1 wei * 1000 bps / 10000 = 0; remainder 1 → holders
    s = split_awp(1, owner_ops_bps=1000)
    assert s.awp_owner_cut == 0
    assert s.awp_to_holders == 1


def test_awp_received_today():
    # balance 100, ownerReserve 30, holderReserve 50 → 20 receivable
    assert awp_received_today(100, 30, 50) == 20


def test_awp_received_drift_raises():
    import pytest
    with pytest.raises(RuntimeError):
        awp_received_today(10, 30, 50)  # balance < reserves


def test_distribute_to_holders_proportional():
    # 100 AWP, alice power 1 / bob power 3 → alice 25, bob 75
    out = distribute_to_holders(
        100,
        {
            "0x1111111111111111111111111111111111111111": 1,
            "0x2222222222222222222222222222222222222222": 3,
        },
    )
    # Last (sorted by addr) absorbs rounding
    assert sum(out.values()) == 100


# --- Settlement integration ---


@dataclass
class _StubSettlementCfg:
    settle_hour_utc: int = 0
    holder_bps: int = 10000
    fusion_bps: int = 0
    owner_ops_bps: int = 1000


@dataclass
class _StubCfg:
    settlement: _StubSettlementCfg


def _new_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    return DB(path), path


def test_compute_day_dual_token():
    cfg = _StubCfg(settlement=_StubSettlementCfg())
    db, _path = _new_db()
    s = Settlement(cfg, db)

    holder_powers = {
        "0x1111111111111111111111111111111111111111": 1,
        "0x2222222222222222222222222222222222222222": 1,
    }
    payload = s.compute_day(1, holder_powers, awp_received=1_000_000)

    # $aArdi: full daily emission, 100% to holders, split 50/50
    assert payload["emission"] == daily_emission(1)
    assert payload["ardi_total"] == daily_emission(1)
    # AWP split: 10% owner cut, 90% to holders
    assert payload["awp_owner_cut"] == 100_000
    assert payload["awp_to_holders"] == 900_000
    # Two leaves, each with both amounts > 0
    assert len(payload["leaves"]) == 2
    for ardi_amt, awp_amt in payload["leaves"].values():
        assert ardi_amt > 0
        assert awp_amt > 0


def test_compute_day_no_holders_redirects_awp_to_owner():
    """If no Ardinals are held, the holder AWP slice routes to ops reserve
    (otherwise it would be stranded forever in the controller balance)."""
    cfg = _StubCfg(settlement=_StubSettlementCfg())
    db, _path = _new_db()
    s = Settlement(cfg, db)

    payload = s.compute_day(1, {}, awp_received=1_000_000)
    assert payload["awp_to_holders"] == 0
    assert payload["awp_owner_cut"] == 1_000_000  # all AWP to ops
    assert payload["leaves"] == {}


def test_store_settlement_dual_token_persistence():
    cfg = _StubCfg(settlement=_StubSettlementCfg())
    db, _path = _new_db()
    s = Settlement(cfg, db)

    holder_powers = {"0x1111111111111111111111111111111111111111": 1}
    payload = s.compute_day(1, holder_powers, awp_received=1_000_000)
    record = s.store_settlement(1, payload)

    assert record["root_hex"].startswith("0x")
    assert record["tx_args"]["day"] == 1
    assert "ardiTotal" in record["tx_args"]
    assert "awpToHolders" in record["tx_args"]
    assert "awpOwnerCut" in record["tx_args"]

    # Persisted row uses the new dual-token columns
    with db.conn() as c:
        row = c.execute(
            "SELECT ardi_total, awp_to_holders, awp_owner_cut FROM daily_settlement WHERE day=?",
            (1,),
        ).fetchone()
    assert row["ardi_total"] == str(payload["ardi_total"])
    assert row["awp_to_holders"] == str(payload["awp_to_holders"])
    assert row["awp_owner_cut"] == str(payload["awp_owner_cut"])
