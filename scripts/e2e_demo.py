#!/usr/bin/env python3
"""
e2e_demo.py — Local integration test: runs the Coordinator + N simulated agents
against the real 21,000 riddle vault, no chain required.

What it exercises:
  1. Vault loading (21,000 entries)
  2. Coordinator opens an epoch (15 riddles selected by rarity weights)
  3. Each agent reads riddles, solves with LLM, submits up to 5 guesses
  4. Coordinator closes epoch, runs verifiable random draw
  5. Coordinator signs mint authorizations for winners
  6. Verify: signatures recover to coordinator address
  7. Stats: solve rates by rarity, draw distribution, signature validity

What it skips (because no chain):
  - Actual on-chain inscribe / fuse calls
  - KYA registration check (mocked to always-true)
  - Bond escrow lock

Run:
    cd ardinals
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 scripts/e2e_demo.py --epochs 2 --agents 4
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Make coordinator importable
sys.path.insert(0, str(Path(__file__).parent.parent / "coordinator" / "src"))

from coordinator.db import DB  # noqa: E402
from coordinator.epoch import EpochEngine, PublishedRiddle  # noqa: E402
from coordinator.signer import Signer, inscribe_digest  # noqa: E402
from coordinator.secure_vault import SecureVault  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402

log = logging.getLogger("e2e")


# ----------------------------- Test config ------------------------------------


@dataclass
class TestConfig:
    chain_id: int = 84532
    contract: str = "0x" + "11" * 20  # mock ArdiNFT address
    epoch_duration: int = 30  # shrink for fast demo (production: 180)
    riddles_per_epoch: int = 15
    max_subs_per_agent: int = 5


def make_engine_cfg(tc: TestConfig):
    """Minimal config object satisfying EpochEngine's expectations."""

    class _Sec:
        pass

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.epoch = _Sec()
    cfg.epoch.duration_seconds = tc.epoch_duration
    cfg.epoch.submission_window = max(1, tc.epoch_duration - 5)
    cfg.epoch.riddles_per_epoch = tc.riddles_per_epoch
    cfg.epoch.max_submissions_per_agent = tc.max_subs_per_agent
    cfg.chain = _Sec()
    cfg.chain.chain_id = tc.chain_id
    cfg.contracts = _Sec()
    cfg.contracts.ardi_nft = tc.contract
    return cfg


# ----------------------------- Simulated agent -------------------------------


@dataclass
class TestAgent:
    name: str
    address: str  # 0x...
    pk: str  # 0x... (used only locally; agents in production would sign txs)


def gen_test_agents(n: int) -> list[TestAgent]:
    """Generate N deterministic test agents."""
    out = []
    for i in range(n):
        pk = "0x" + (f"{i + 1:02x}" * 32)[:64]
        acct = Account.from_key(pk)
        out.append(TestAgent(name=f"agent{i}", address=acct.address, pk=pk))
    return out


async def solve_with_claude(riddles: list[PublishedRiddle], top_k: int) -> dict[int, str]:
    """Use `claude -p` CLI to solve `top_k` highest-EV riddles. Returns {wordId: best_guess}."""
    rarity_w = {"legendary": 4.0, "rare": 2.0, "uncommon": 1.3, "common": 1.0}
    sorted_rs = sorted(
        riddles, key=lambda r: r.power * rarity_w.get(r.rarity, 1.0), reverse=True
    )[:top_k]

    payload = [
        {"id": r.word_id, "language": r.language, "riddle": r.riddle}
        for r in sorted_rs
    ]
    prompt = (
        "Solve these multilingual word riddles. For each, give your single best "
        "guess in the riddle's TARGET language (CJK chars for zh/ja/ko, accented "
        "Latin for fr/de, plain for en). NEVER translate to English.\n\n"
        "OUTPUT: single JSON array, no preamble, no markdown.\n"
        'Each item: {"id": int, "guess": "..."}\n\n'
        f"Riddles:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--model", "sonnet",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {}

    text = out.decode()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```", "", text)
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        log.warning(f"no JSON in claude output: {text[:200]}")
        return {}
    try:
        items = json.loads(m.group(0))
        return {int(it["id"]): str(it["guess"]) for it in items}
    except Exception as e:
        log.warning(f"json parse fail: {e}")
        return {}


# ----------------------------- Demo loop -------------------------------------


async def run_one_epoch(engine: EpochEngine, agents: list[TestAgent], top_k: int):
    state = engine.open_epoch()
    log.info(f"--- Epoch {state.epoch_id} opened with {len(state.riddles)} riddles ---")
    for r in state.riddles:
        log.info(f"  riddle word_id={r.word_id} lang={r.language} rarity={r.rarity} power={r.power}")

    # Each agent solves + submits in parallel
    async def agent_act(agent: TestAgent):
        guesses = await solve_with_claude(state.riddles, top_k)
        with engine.db.conn() as c:
            for wid, g in guesses.items():
                try:
                    c.execute(
                        "INSERT INTO submissions "
                        "(epoch_id, word_id, agent, submission, submitted_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (state.epoch_id, wid, agent.address.lower(), g, int(time.time())),
                    )
                except Exception:
                    pass
        log.info(f"  {agent.name} ({agent.address[:10]}): submitted {len(guesses)} guesses")
        return guesses

    all_guesses = await asyncio.gather(*(agent_act(a) for a in agents))
    return state, all_guesses


def close_and_verify(engine: EpochEngine, state, signer: Signer, tc: TestConfig):
    auths = engine.close_and_draw(state.epoch_id)
    log.info(f"  Coordinator drew {len(auths)} winners")

    if not auths:
        return {"epoch": state.epoch_id, "winners": 0, "all_valid": True}

    # Verify each authorization signature recovers to coordinator address
    all_valid = True
    summary = []
    for a in auths:
        digest = inscribe_digest(
            chain_id=tc.chain_id,
            contract=tc.contract,
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
        ok = recovered.lower() == signer.address.lower()
        all_valid = all_valid and ok
        summary.append({
            "word_id": a["wordId"],
            "word": a["word"],
            "agent": a["agent"][:10] + "...",
            "sig_valid": ok,
        })
        log.info(f"    word_id={a['wordId']} word={a['word']!r} winner={a['agent'][:10]}... sig_ok={ok}")

    return {"epoch": state.epoch_id, "winners": len(auths), "all_valid": all_valid, "details": summary}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=str(Path(__file__).parent.parent / "data" / "riddles.json"))
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--top-k", type=int, default=5, help="riddles each agent attempts per epoch")
    ap.add_argument("--epoch-duration", type=int, default=30)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(message)s")

    tc = TestConfig(epoch_duration=args.epoch_duration)

    # Coordinator state
    db_path = tempfile.mktemp(suffix="_e2e.db")
    db = DB(db_path)
    log.info(f"DB at {db_path}")

    vault = SecureVault(args.vault)  # plaintext mode for demo
    log.info(f"Vault: {len(vault)} entries")

    # Generate coordinator signer
    coord_pk = "0x" + "AB" * 32
    signer = Signer(coord_pk)
    log.info(f"Coordinator signer: {signer.address}")

    cfg = make_engine_cfg(tc)
    engine = EpochEngine(cfg, db, vault, signer)

    agents = gen_test_agents(args.agents)
    log.info(f"Test agents: {len(agents)}")
    for a in agents:
        log.info(f"  {a.name}: {a.address}")

    results = []
    for ep in range(args.epochs):
        log.info(f"\n==== Epoch {ep + 1}/{args.epochs} ====")
        state, _ = await run_one_epoch(engine, agents, args.top_k)
        result = close_and_verify(engine, state, signer, tc)
        results.append(result)
        log.info(f"Epoch {state.epoch_id} done: {result['winners']} winners, "
                 f"all signatures valid={result['all_valid']}")

    # Final summary
    print("\n" + "=" * 60)
    print("E2E DEMO SUMMARY")
    print("=" * 60)
    total_winners = sum(r["winners"] for r in results)
    all_valid = all(r["all_valid"] for r in results)
    print(f"Epochs run         : {len(results)}")
    print(f"Total mint winners : {total_winners}")
    print(f"All sigs valid     : {all_valid}")
    print(f"DB                 : {db_path}")
    print()
    print("Per-epoch breakdown:")
    for r in results:
        print(f"  epoch {r['epoch']}: {r['winners']} winners, sigs ok={r['all_valid']}")


if __name__ == "__main__":
    asyncio.run(main())
