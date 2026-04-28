#!/usr/bin/env python3
"""
fusion_precheck.py — pre-launch sweep that asks the LLM to fuse N random
pairs from the vault and flags any suggested_word that collides with the
vault. Anything that collides means the LLM rule "produce a phrase / new
concept, never a vault word" is being violated and we need to refine the
prompt before going live.

Run:
    python3 scripts/fusion_precheck.py --pairs 200
    python3 scripts/fusion_precheck.py --pairs 5000 --workers 4   # bigger sweep
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "coordinator" / "src"))

from coordinator.vault import Vault  # noqa: E402

LANG_REV = {0: "en", 1: "zh", 2: "ja", 3: "ko", 4: "fr", 5: "de"}


# Stronger prompt with explicit anti-collision instruction
SYSTEM_PROMPT = """You are a fusion oracle for the Ardi WorkNet inscription system.

Given two words from possibly different languages, evaluate their SEMANTIC COMPATIBILITY on a [0, 1] scale, then produce:
  - compatibility (float 0..1)
  - suggested_word: a new word, phrase, or compound for the fusion product
  - suggested_language: one of {en, zh, ja, ko, fr, de}
  - rationale: one-sentence justification

CRITICAL RULES for suggested_word:
  - It MUST be DISTINCT from both parent words (case-insensitive).
  - It MUST be a creative phrase, compound, or descriptor — NOT a single
    common dictionary word that already exists in any language.
  - Prefer multi-word phrases ("inverse echo", "midnight alchemy") or
    coined compounds that capture the unique character of the fusion.
  - DO NOT default to obvious single words like "blockchain", "fire",
    "love" — those are common nouns that already exist as inscriptions.

Output a single JSON object, no preamble, no markdown."""


def normalize(s: str) -> str:
    """NFKC-lowercase normalize for collision checking."""
    return unicodedata.normalize("NFKC", s.strip().lower())


async def llm_evaluate(word_a, lang_a, word_b, lang_b, model="sonnet"):
    user = (
        f"Word A: {word_a} (language: {lang_a})\n"
        f"Word B: {word_b} (language: {lang_b})\n\n"
        "Output JSON: {compatibility, suggested_word, suggested_language, rationale}."
    )
    full = f"{SYSTEM_PROMPT}\n\n{user}"

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", full, "--model", model,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None

    text = out.decode()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```", "", text)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def check_pair(vault_set: set[str], a, b, sem) -> dict:
    async with sem:
        result = await llm_evaluate(a["word"], a["language"], b["word"], b["language"])
    if result is None:
        return {"a": a, "b": b, "status": "llm_error"}
    suggested = result.get("suggested_word", "")
    sn = normalize(suggested)
    a_norm = normalize(a["word"])
    b_norm = normalize(b["word"])

    # Collision checks
    collisions = []
    if sn in vault_set:
        collisions.append("vault_word")
    if sn == a_norm or sn == b_norm:
        collisions.append("parent_word")
    if " " not in sn and len(sn) < 8 and not any("一" <= c <= "鿿" for c in sn):
        # single short non-CJK word — likely a common dictionary word
        collisions.append("likely_dictionary_word")

    return {
        "a": a, "b": b,
        "compatibility": result.get("compatibility"),
        "suggested": suggested,
        "rationale": result.get("rationale", ""),
        "collisions": collisions,
        "status": "ok" if not collisions else "collision",
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=str(Path(__file__).parent.parent / "data" / "riddles.json"))
    ap.add_argument("--pairs", type=int, default=200)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="fusion_precheck_results.json")
    args = ap.parse_args()

    vault = Vault(args.vault)
    print(f"Loaded vault: {len(vault)} entries")

    # Build lowercase set for collision detection
    vault_words = {normalize(vault.get(i).word) for i in range(len(vault))}

    rng = random.Random(args.seed)
    entries = [
        {"word_id": i, "word": vault.get(i).word,
         "language": vault.get(i).language, "power": vault.get(i).power,
         "rarity": vault.get(i).rarity}
        for i in range(len(vault))
    ]

    pairs = []
    for _ in range(args.pairs):
        a, b = rng.sample(entries, 2)
        pairs.append((a, b))

    print(f"Testing {len(pairs)} random pairs with {args.workers} concurrent workers")

    sem = asyncio.Semaphore(args.workers)
    t0 = time.monotonic()
    results = await asyncio.gather(*(check_pair(vault_words, a, b, sem) for a, b in pairs))
    dt = time.monotonic() - t0

    # Summarize
    status_counts = Counter(r["status"] for r in results)
    collision_types = Counter()
    for r in results:
        for c in r.get("collisions", []):
            collision_types[c] += 1

    collisions = [r for r in results if r["status"] == "collision"]

    print()
    print("=" * 60)
    print(f"FUSION PRECHECK ({dt / 60:.1f} min wall)")
    print("=" * 60)
    print(f"Total pairs            : {len(results)}")
    print(f"OK (no collision)      : {status_counts['ok']}")
    print(f"Collisions             : {status_counts['collision']}")
    print(f"LLM errors             : {status_counts['llm_error']}")
    print(f"Pass rate              : {status_counts['ok'] / len(results) * 100:.1f}%")
    print()
    print("Collision breakdown:")
    for k, v in collision_types.most_common():
        print(f"  {k:<28} {v}")

    if collisions:
        print()
        print(f"Sample collisions (first 10 of {len(collisions)}):")
        for r in collisions[:10]:
            a, b = r["a"], r["b"]
            print(f"  {a['word']:<14} ({a['language']}) + {b['word']:<14} ({b['language']})  →  "
                  f"{r['suggested']!r:<25}  [{', '.join(r['collisions'])}]")

    # Persist full results
    with open(args.out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nFull results → {args.out}")

    print()
    if status_counts["collision"] / len(results) > 0.05:
        print("⚠️  Collision rate > 5% — refine the prompt before launch.")
        sys.exit(1)
    else:
        print("✓ Collision rate within tolerance.")


if __name__ == "__main__":
    asyncio.run(main())
