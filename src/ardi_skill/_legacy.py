#!/usr/bin/env python3
"""
[LEGACY V1 — DEPRECATED 2026-04 — DO NOT USE FOR NEW DEPLOYMENTS]

This file references the old off-chain submit flow that was replaced by
on-chain commit-reveal in v0.2. It only exists for archaeological reference
and may not work against current Coordinator versions (the /v1/submit
endpoint was removed in commit ab11cb4). For the current commit-reveal
flow use:
  - SDK:     src/ardi_sdk.py
  - Example: examples/full_cycle.py

Original docstring follows.
========================================================================

ardi agent — reference implementation.

Loop:
    1. Fetch current epoch from Coordinator
    2. Pick top-5 riddles by expected value
    3. Solve via configurable LLM backend
    4. Submit guesses
    5. After close, fetch authorizations and submit on-chain inscribe txs
    6. Repeat until capped (3 mints) or mining sealed

USAGE:
    python3 agent.py \
        --coordinator https://api.ardi.work \
        --agent-pk $ARDI_AGENT_PK \
        --rpc https://mainnet.base.org \
        --ardi-nft 0x... \
        --bond-escrow 0x... \
        --solver claude    # or 'openai', 'custom'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("ardi.agent")


@dataclass
class Riddle:
    word_id: int
    riddle: str
    power: int
    rarity: str
    language: str
    language_id: int


# ------------------------- Solvers -------------------------


async def solve_with_claude(riddles: list[Riddle], model: str = "sonnet") -> list[dict]:
    """Use `claude -p` CLI. One batched call. Returns up to 3 ranked guesses per riddle."""
    payload = [
        {"id": r.word_id, "language": r.language, "riddle": r.riddle}
        for r in riddles
    ]
    prompt = (
        "Solve these word riddles. For each, give 3 ranked guesses in the riddle's "
        "TARGET language (CJK chars for zh/ja/ko; accented Latin for fr/de; plain for en). "
        "Best guess first. No translation.\n\n"
        "OUTPUT: single JSON array, no preamble, no markdown.\n"
        'Each item: {"id": int, "guesses": [str, str, str]}\n\n'
        f"Riddles:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--model", model,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    text = out.decode()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```", "", text)
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        raise RuntimeError(f"no JSON in claude response: {text!r}")
    return json.loads(m.group(0))


async def solve_with_openai(riddles: list[Riddle], model: str = "gpt-4o-mini") -> list[dict]:
    """Use OpenAI API."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    payload = [
        {"id": r.word_id, "language": r.language, "riddle": r.riddle}
        for r in riddles
    ]
    prompt = (
        "Solve these word riddles. For each, give 3 ranked guesses in the riddle's "
        "TARGET language. Best first. No translation. Return raw JSON only.\n\n"
        "Output: array of {id: int, guesses: [str, str, str]}.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content).get("results", [])


# ------------------------- EV ranking -------------------------


def expected_value(riddle: Riddle) -> float:
    """Crude EV: power × rarity weight × confidence prior."""
    rarity_mult = {"legendary": 4.0, "rare": 2.0, "uncommon": 1.3, "common": 1.0}.get(
        riddle.rarity, 1.0
    )
    return riddle.power * rarity_mult


def select_top5(riddles: list[Riddle]) -> list[Riddle]:
    return sorted(riddles, key=expected_value, reverse=True)[:5]


# ------------------------- Coordinator client -------------------------


class CoordinatorClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    async def current_epoch(self) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}/v1/epoch/current", timeout=10.0)
            r.raise_for_status()
            return r.json()

    async def submit(self, agent: str, signature: str, guesses: list[dict]) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self.base}/v1/submit",
                json={"agent": agent, "signature": signature, "guesses": guesses},
                timeout=10.0,
            )
            r.raise_for_status()
            return r.json()

    async def get_authorizations(self, epoch_id: int, agent: str) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{self.base}/v1/auth/{epoch_id}/{agent}",
                timeout=10.0,
            )
            r.raise_for_status()
            return r.json()

    async def agent_state(self, agent: str) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}/v1/agent/{agent}/state", timeout=10.0)
            r.raise_for_status()
            return r.json()


# ------------------------- Main loop -------------------------


async def run(args):
    client = CoordinatorClient(args.coordinator)
    solver = {
        "claude": solve_with_claude,
        "openai": solve_with_openai,
    }[args.solver]

    while True:
        try:
            state = await client.agent_state(args.agent_addr)
            if state["mintCount"] >= 3:
                log.info("mint cap reached (3) — stopping")
                break

            epoch = await client.current_epoch()
            now = int(time.time())
            deadline = epoch["submissionDeadline"]
            remaining = deadline - now
            if remaining <= 5:
                log.info(f"epoch {epoch['epochId']}: too late, waiting next")
                await asyncio.sleep(remaining + 2)
                continue

            log.info(f"epoch {epoch['epochId']}: {len(epoch['riddles'])} riddles, "
                     f"{remaining}s to deadline")
            riddles = [Riddle(**{
                "word_id": r["wordId"],
                "riddle": r["riddle"],
                "power": r["power"],
                "rarity": r["rarity"],
                "language": r["language"],
                "language_id": r["languageId"],
            }) for r in epoch["riddles"]]

            picks = select_top5(riddles)
            log.info(f"picked top-{len(picks)} by EV: " +
                     ", ".join(f"#{r.word_id}({r.rarity}/p{r.power})" for r in picks))

            answers = await solver(picks)
            by_id = {a["id"]: a.get("guesses", []) for a in answers}

            guesses = [
                {"wordId": r.word_id, "guess": by_id.get(r.word_id, [""])[0]}
                for r in picks
                if by_id.get(r.word_id)
            ]
            res = await client.submit(args.agent_addr, "0xPLACEHOLDER", guesses)
            log.info(f"submitted {len(res['accepted'])}/{len(guesses)} accepted")

            # Wait for epoch close + small reveal margin
            epoch_close = epoch["endTs"]
            await asyncio.sleep(max(1, epoch_close - int(time.time()) + 5))

            auths = await client.get_authorizations(epoch["epochId"], args.agent_addr)
            wins = auths.get("authorizations", [])
            log.info(f"epoch {epoch['epochId']}: won {len(wins)} draws")

            for win in wins:
                # Submit on-chain inscribe tx
                # Production: use web3.py with args.agent_pk to call ardi_nft.inscribe(...)
                log.info(f"would mint wordId={win['wordId']} word={win['word']!r}")

        except Exception as e:
            log.error(f"loop error: {e}", exc_info=True)
            await asyncio.sleep(10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coordinator", default=os.environ.get("ARDI_COORDINATOR_URL", "https://api.ardi.work"))
    ap.add_argument("--agent-addr", required=True, help="0x... ethereum address of this agent")
    ap.add_argument("--agent-pk", default=os.environ.get("ARDI_AGENT_PK", ""))
    ap.add_argument("--rpc", default=os.environ.get("RPC_URL", ""))
    ap.add_argument("--ardi-nft", default="")
    ap.add_argument("--bond-escrow", default="")
    ap.add_argument("--solver", choices=["claude", "openai"], default="claude")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
