#!/usr/bin/env python3
"""
perf_test.py — Coordinator performance benchmark.

Measures the engine's raw throughput at the layer that matters: opening an
epoch, ingesting N agents × 5 submissions, closing the epoch (filter correct
+ random draw), and signing 15 mint authorizations.

This is the actual production hot-path. HTTP framing is excluded so we
isolate engine + database + crypto.

Scales tested: 100 / 1k / 5k / 10k agents.

Run:
    python3 scripts/perf_test.py
    python3 scripts/perf_test.py --scales 100,1000,5000,10000 --hit-rate 0.6
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "coordinator" / "src"))

from coordinator.db import DB  # noqa: E402
from coordinator.epoch import EpochEngine, PublishedRiddle  # noqa: E402
from coordinator.signer import Signer, inscribe_digest  # noqa: E402
from coordinator.vault import Vault  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402

log = logging.getLogger("perf")


def make_cfg(epoch_duration=180):
    class _S:
        pass

    cfg = _S()
    cfg.epoch = _S()
    cfg.epoch.duration_seconds = epoch_duration
    cfg.epoch.submission_window = epoch_duration - 15
    cfg.epoch.riddles_per_epoch = 15
    cfg.epoch.max_submissions_per_agent = 5
    cfg.chain = _S()
    cfg.chain.chain_id = 84532
    cfg.contracts = _S()
    cfg.contracts.ardi_nft = "0x" + "11" * 20
    return cfg


def gen_agent_addrs(n: int) -> list[str]:
    """Generate N deterministic ethereum addresses from indices."""
    return [Account.from_key("0x" + (f"{i + 1:08x}" * 8)[:64]).address.lower() for i in range(n)]


@dataclass
class PhaseTime:
    name: str
    seconds: float

    def __str__(self):
        return f"{self.name:<28} {self.seconds * 1000:>8.1f} ms"


def run_scenario(vault: Vault, n_agents: int, hit_rate: float, vault_path: str) -> dict:
    """Run one full epoch scenario with n_agents and report timings."""
    db_path = tempfile.mktemp(suffix=f"_perf_{n_agents}.db")
    db = DB(db_path)
    signer = Signer("0x" + "AB" * 32)
    cfg = make_cfg()
    engine = EpochEngine(cfg, db, vault, signer)

    timings: list[PhaseTime] = []

    # ---- Phase 1: open epoch ----
    t = time.monotonic()
    state = engine.open_epoch()
    timings.append(PhaseTime("open epoch", time.monotonic() - t))

    # ---- Phase 2: generate agents (off-clock, just setup) ----
    t = time.monotonic()
    addresses = gen_agent_addrs(n_agents)
    setup_time = time.monotonic() - t

    # ---- Phase 3: submit (THE hot path) ----
    # Each agent picks 5 of 15 riddles, hit_rate% are correct
    rng = random.Random(42)
    riddles = state.riddles
    truth_by_id = {r.word_id: vault.reveal_word(r.word_id) for r in riddles}

    t = time.monotonic()
    with db.conn() as c:
        rows = []
        now = int(time.time())
        for addr in addresses:
            picks = rng.sample(riddles, k=min(5, len(riddles)))
            for r in picks:
                truth = truth_by_id[r.word_id]
                # hit_rate% chance of correct answer
                guess = truth if rng.random() < hit_rate else f"wrong_{rng.randint(0, 999999)}"
                rows.append((state.epoch_id, r.word_id, addr, guess, now))
        # Bulk insert all submissions
        c.executemany(
            "INSERT OR IGNORE INTO submissions "
            "(epoch_id, word_id, agent, submission, submitted_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    submit_time = time.monotonic() - t
    timings.append(PhaseTime(f"submit {len(rows)} rows", submit_time))

    # ---- Phase 4: close + draw + sign ----
    t = time.monotonic()
    auths = engine.close_and_draw(state.epoch_id)
    close_time = time.monotonic() - t
    timings.append(PhaseTime(f"close+draw+sign ({len(auths)} winners)", close_time))

    # ---- Phase 5: verify all signatures ----
    t = time.monotonic()
    all_valid = True
    for a in auths:
        digest = inscribe_digest(
            chain_id=cfg.chain.chain_id,
            contract=cfg.contracts.ardi_nft,
            word_id=a["wordId"],
            word=a["word"],
            power=a["power"],
            language_id=a["languageId"],
            agent=a["agent"],
            epoch_id=a["epochId"],
        )
        msg = encode_defunct(primitive=digest)
        sig_bytes = bytes.fromhex(a["signature"][2:])
        recovered = Account.recover_message(msg, signature=sig_bytes)
        if recovered.lower() != signer.address.lower():
            all_valid = False
            break
    verify_time = time.monotonic() - t
    timings.append(PhaseTime(f"verify {len(auths)} sigs", verify_time))

    total_hot_path = submit_time + close_time

    # cleanup
    os.unlink(db_path)

    return {
        "n_agents": n_agents,
        "n_submissions": len(rows),
        "n_winners": len(auths),
        "all_sigs_valid": all_valid,
        "submit_seconds": submit_time,
        "close_seconds": close_time,
        "verify_seconds": verify_time,
        "total_hot_seconds": total_hot_path,
        "submissions_per_sec": len(rows) / submit_time if submit_time > 0 else 0,
        "timings": timings,
        "setup_seconds": setup_time,
    }


def print_report(results: list[dict], epoch_duration: int):
    print("\n" + "=" * 88)
    print(f"{'agents':>8} {'subs':>8} {'wins':>5} {'submit':>10} {'close':>10} "
          f"{'sub/sec':>10} {'epoch%':>8} {'verdict':>10}")
    print("-" * 88)
    for r in results:
        ratio = r["total_hot_seconds"] / epoch_duration * 100
        verdict = "OK" if r["total_hot_seconds"] < epoch_duration * 0.5 else "TIGHT"
        if r["total_hot_seconds"] > epoch_duration:
            verdict = "FAIL"
        print(
            f"{r['n_agents']:>8} "
            f"{r['n_submissions']:>8} "
            f"{r['n_winners']:>5} "
            f"{r['submit_seconds'] * 1000:>8.1f}ms "
            f"{r['close_seconds'] * 1000:>8.1f}ms "
            f"{r['submissions_per_sec']:>10.0f} "
            f"{ratio:>7.2f}% "
            f"{verdict:>10}"
        )
    print("=" * 88)
    print(f"epoch budget: {epoch_duration}s ({epoch_duration * 1000}ms)")
    print(f"hot path = submit + close+draw+sign")
    print(f"epoch% = hot path as fraction of epoch duration (lower is better)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--vault",
        default=str(Path(__file__).parent.parent / "data" / "riddles.json"),
        help="path to 21k riddle vault",
    )
    ap.add_argument(
        "--scales",
        default="100,1000,5000,10000",
        help="comma-separated agent counts",
    )
    ap.add_argument(
        "--hit-rate",
        type=float,
        default=0.6,
        help="fraction of submissions that are correct (default 60%)",
    )
    ap.add_argument(
        "--epoch-duration",
        type=int,
        default=180,
        help="epoch duration in seconds (production = 180)",
    )
    ap.add_argument(
        "--detailed",
        action="store_true",
        help="show per-phase breakdown for each scale",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    print("Loading vault...")
    vault = Vault(args.vault)
    print(f"  loaded {len(vault)} entries\n")

    scales = [int(s) for s in args.scales.split(",")]
    print(f"Running scales: {scales}, hit_rate={args.hit_rate}\n")

    results = []
    for n in scales:
        print(f"  scale {n:>6}... ", end="", flush=True)
        t0 = time.monotonic()
        r = run_scenario(vault, n, args.hit_rate, args.vault)
        wall = time.monotonic() - t0
        print(f"submit {r['submit_seconds']:.2f}s, "
              f"close {r['close_seconds']:.2f}s, "
              f"wall {wall:.1f}s")
        results.append(r)

        if args.detailed:
            for t in r["timings"]:
                print(f"    {t}")

    print_report(results, args.epoch_duration)

    # Sanity check
    if not all(r["all_sigs_valid"] for r in results):
        print("\n!!! signature validation failed at some scale !!!")
        sys.exit(1)


if __name__ == "__main__":
    main()
