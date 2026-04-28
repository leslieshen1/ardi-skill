"""Scan the 21K-word vault for cross-language false friends.

A "false friend" is a pair of words that have the same spelling across two
languages but different meanings — `parole` (en, "release on parole") vs
`parole` (fr, "speech / spoken word") is the canonical example.

LLM oracle should classify these as T4 or T5, not T1, but the human-readable
hint is useful for:
  - prompt engineering (we can tell the LLM "these are flagged false friends")
  - audit trail (proves we surveyed the vault for this edge case)
  - vault quality (very high false-friend density would suggest revisiting
    word selection)

Output: scripts/false_friends_report.json — a list of candidate pairs ranked
by how likely they are to be misleading. Final classification is still up to
human review (this is a heuristic, not a definitive judgment).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
VAULT_PATH = ROOT / "data" / "riddles.json"


def main():
    vault = json.loads(VAULT_PATH.read_text())
    print(f"vault: {len(vault)} entries")

    # Group by lowercase word
    by_word: dict[str, list[dict]] = defaultdict(list)
    for entry in vault:
        by_word[entry["word"].lower()].append(entry)

    # A "candidate false friend" is a word that appears in 2+ languages.
    # We flag them all; humans (or the LLM) decide which are *misleading*.
    candidates = [
        {"word": w, "entries": [
            {"word_id": vault.index(e), "language": e["language"],
             "power": e["power"], "rarity": e["rarity"], "riddle": e["riddle"]}
            for e in entries
        ]}
        for w, entries in by_word.items() if len(entries) >= 2
    ]

    # Sort by number of distinct languages (more = more likely false friend)
    candidates.sort(key=lambda c: -len({e["language"] for e in c["entries"]}))

    print(f"\nfalse-friend candidates: {len(candidates)} words appear in ≥2 languages")
    print()
    print(f"{'word':<24} {'#lang':>5}  riddles")
    print("-" * 78)
    for c in candidates[:30]:
        langs = sorted({e["language"] for e in c["entries"]})
        print(f"  {c['word']:<22} {len(langs):>4}  {'/'.join(langs)}")
        for e in c["entries"][:4]:
            riddle_short = e["riddle"][:60].replace("\n", " ")
            print(f"    [{e['language']}] {riddle_short}")
        print()

    # Persist for use in prompt engineering / vault review
    out_path = ROOT / "scripts" / "false_friends_report.json"
    out_path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False))
    print(f"\nFull report: {out_path}")
    print(f"  total candidate words: {len(candidates)}")
    print(f"  candidate pair count:  {sum(len(c['entries']) * (len(c['entries']) - 1) // 2 for c in candidates)}")


if __name__ == "__main__":
    main()
