#!/usr/bin/env python3
"""
fusion_demo.py — Run the fusion oracle on real word pairs and show
how the LLM's compatibility score maps to game mechanics.

Uses `claude -p` CLI directly (no API key needed if Claude Code installed).

Run:
    python3 scripts/fusion_demo.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

LANG_REV = {0: "en", 1: "zh", 2: "ja", 3: "ko", 4: "fr", 5: "de"}
LANG_MAP = {v: k for k, v in LANG_REV.items()}

SYSTEM_PROMPT = """You are a fusion oracle for the Ardi WorkNet inscription system.

Given two words from possibly different languages, evaluate their SEMANTIC COMPATIBILITY on a [0, 1] scale, then produce:
  - compatibility (float 0..1)
  - suggested_word: a new word that semantically combines or arises from the union of the two
  - suggested_language: one of {en, zh, ja, ko, fr, de}
  - rationale: one-sentence justification

Output a single JSON object, no preamble, no markdown."""


def _multiplier(comp: float) -> float:
    if comp > 0.8: return 1.5
    if comp >= 0.6: return 2.0
    if comp >= 0.3: return 2.5
    return 3.0


def _success_rate(comp: float) -> float:
    return 0.20 + comp * 0.50


def _pair_key(wA, lA, wB, lB) -> str:
    a = f"{lA}:{wA}"
    b = f"{lB}:{wB}"
    return f"{a}||{b}" if a <= b else f"{b}||{a}"


def _roll_success(comp: float, key: str) -> bool:
    h = hashlib.sha256(key.encode()).digest()
    roll = int.from_bytes(h[:8], "big") / (1 << 64)
    return roll < _success_rate(comp)


async def call_llm(word_a, lang_a, word_b, lang_b):
    user_prompt = (
        f"Word A: {word_a} (language: {lang_a})\n"
        f"Word B: {word_b} (language: {lang_b})\n\n"
        "Output a single JSON object with keys: compatibility (float 0..1), "
        "suggested_word (string), suggested_language (one of en|zh|ja|ko|fr|de), "
        "rationale (one sentence)."
    )
    full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", full_prompt, "--model", "sonnet",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    text = out.decode()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```", "", text)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"no JSON in LLM output: {text[:200]}")
    return json.loads(m.group(0))


async def evaluate(word_a, lang_a, power_a, word_b, lang_b, power_b):
    print(f"\n{'─' * 70}")
    print(f"  A: {word_a:<14} ({lang_a}, power {power_a})")
    print(f"  B: {word_b:<14} ({lang_b}, power {power_b})")
    print(f"{'─' * 70}")

    # 1. LLM evaluates
    result = await call_llm(word_a, lang_a, word_b, lang_b)

    comp = float(result["compatibility"])
    suggested = result["suggested_word"]
    suggested_lang = result.get("suggested_language", "en")
    rationale = result.get("rationale", "")

    print(f"  LLM compatibility   : {comp:.2f}")
    print(f"  LLM suggested word  : {suggested!r} ({suggested_lang})")
    print(f"  LLM rationale       : {rationale}")

    # 2. Game mechanics
    mult = _multiplier(comp)
    sr = _success_rate(comp)
    key = _pair_key(word_a, LANG_MAP.get(lang_a, 0), word_b, LANG_MAP.get(lang_b, 0))
    success = _roll_success(comp, key)
    new_power = int((power_a + power_b) * mult) if success else 0

    print()
    print(f"  → multiplier         : {mult}× (lower compat = higher reward)")
    print(f"  → success_rate       : {sr * 100:.1f}%")
    print(f"  → deterministic roll : {'SUCCESS' if success else 'FAILURE'} (sha256-derived)")
    if success:
        print(f"  → new ardinal        : {suggested!r} with power {new_power}")
    else:
        print(f"  → outcome            : burn the lower-power parent (keep {max(power_a, power_b)})")


async def main():
    pairs = [
        ("fire",     "en", 80,  "water",   "en", 60),   # classic, high compat
        ("fire",     "en", 80,  "火",      "zh", 78),   # cross-lingual same concept
        ("bitcoin",  "en", 100, "ethereum","en", 95),   # crypto siblings
        ("dream",    "en", 92,  "夢",      "ja", 90),   # multilingual same concept
        ("bitcoin",  "en", 100, "土豆",     "zh", 20),   # absurd (potato)
        ("love",     "en", 92,  "war",     "en", 78),   # opposites — interesting
        ("dragon",   "en", 88,  "phoenix", "en", 88),   # mythic siblings
    ]

    for wA, lA, pA, wB, lB, pB in pairs:
        try:
            await evaluate(wA, lA, pA, wB, lB, pB)
        except Exception as e:
            print(f"  ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(main())
