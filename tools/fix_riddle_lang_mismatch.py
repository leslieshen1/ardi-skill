#!/usr/bin/env python3
"""fix_riddle_lang_mismatch.py — repair riddle/answer language mismatches.

Scans data/riddles.json for entries where the riddle text is in a different
script from the declared language (e.g. Korean answer with English riddle).
Regenerates the riddle in the correct language via `claude -p`, validates
the output, and writes the file back atomically.

Usage:
  python3 tools/fix_riddle_lang_mismatch.py --dry-run    # report only
  python3 tools/fix_riddle_lang_mismatch.py              # actually fix
  python3 tools/fix_riddle_lang_mismatch.py --limit 5    # fix first 5 only

The file's array index = wordId, so length & order are preserved.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
RIDDLES = ROOT / "data" / "riddles.json"

EXPECTED_SCRIPTS = {
    "en": {"latin"},
    "fr": {"latin", "latin-ext"},
    "de": {"latin", "latin-ext"},
    "zh": {"cjk"},
    "ja": {"kana", "cjk"},   # ja allows kana, kanji, or both
    "ko": {"hangul"},
}

LANG_NAME = {
    "en": "English", "fr": "French", "de": "German",
    "zh": "Chinese (Simplified)", "ja": "Japanese", "ko": "Korean",
}


def script_of(text: str) -> set[str]:
    scripts = set()
    for ch in text:
        if not ch.strip() or ch in '.,!?;:()[]{}\'"-—–·、。，！？「」『』《》（）':
            continue
        cp = ord(ch)
        if 0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF:
            scripts.add("kana")
        elif 0xAC00 <= cp <= 0xD7AF:
            scripts.add("hangul")
        elif 0x4E00 <= cp <= 0x9FFF:
            scripts.add("cjk")
        elif ch.isalpha() and ch.isascii():
            scripts.add("latin")
        elif ch.isalpha() and not ch.isascii():
            scripts.add("latin-ext")
    return scripts


def riddle_lang_mismatch(entry: dict) -> bool:
    rs = script_of(entry["riddle"])
    expected = EXPECTED_SCRIPTS.get(entry["language"], set())
    return not bool(rs & expected)


PROMPT_TMPL = """You are rewriting a riddle so it is fully in {lang_name}.

The current riddle is in the wrong language. Rewrite it in {lang_name} only.

Answer (the word being riddled):
  "{word}"

Difficulty: {rarity} (power={power})

Original (wrong-language) riddle for semantic reference:
  "{old_riddle}"

CRITICAL CONSTRAINTS — read carefully:
1. Output ONLY the new riddle text, nothing else (no quotes, no commentary, no preamble).
2. Use {lang_name} script exclusively. No English, no other languages.
3. The new riddle MUST NOT contain the substring "{word}" anywhere — not even
   as part of a larger word. Verify your output before responding. If the
   answer would naturally appear, rewrite to avoid it (use synonyms, indirect
   description, antonyms, metaphor, or context).
4. Do NOT include obvious literal translations of "{word}" either.
5. Length: 1-3 sentences, similar to the original.
6. Tone: poetic for legendary/rare, plainer for common.

Now write the {lang_name} riddle (and double-check constraint 3 before outputting):"""


def call_claude(prompt: str, retries: int = 3) -> str:
    """Invoke claude -p; returns stripped text. Raises on persistent failure."""
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True, text=True, timeout=90,
            )
            out = r.stdout.strip()
            if out and "Execution error" not in out:
                # strip surrounding quotes if model wrapped output
                if (out.startswith('"') and out.endswith('"')) or \
                   (out.startswith("'") and out.endswith("'")):
                    out = out[1:-1].strip()
                return out
        except subprocess.TimeoutExpired:
            pass
        if attempt < retries - 1:
            time.sleep(2)
    raise RuntimeError("claude -p failed after retries")


def validate_new_riddle(new: str, entry: dict) -> tuple[bool, str]:
    """Returns (ok, reason). Checks language and word-leak."""
    if len(new) < 10:
        return False, f"too short ({len(new)} chars)"
    if entry["word"].lower() in new.lower():
        return False, f"contains the answer word '{entry['word']}'"
    rs = script_of(new)
    expected = EXPECTED_SCRIPTS.get(entry["language"], set())
    if not (rs & expected):
        return False, f"still wrong script: {rs} vs expected {expected}"
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="only report, don't write")
    ap.add_argument("--limit", type=int, help="only fix first N mismatches")
    args = ap.parse_args()

    data = json.loads(RIDDLES.read_text())
    mismatches = [(i, r) for i, r in enumerate(data) if riddle_lang_mismatch(r)]
    print(f"found {len(mismatches)} riddle-language mismatches in {len(data)} entries")

    if args.dry_run:
        for i, r in mismatches[:10]:
            print(f"  wordId={i:5d}  lang={r['language']}  word='{r['word'][:20]}'  "
                  f"riddle_scripts={script_of(r['riddle'])}")
        if len(mismatches) > 10:
            print(f"  ... and {len(mismatches)-10} more")
        return

    targets = mismatches[: args.limit] if args.limit else mismatches
    print(f"\nrewriting {len(targets)} riddles via claude -p…\n")

    fixed = []
    failed = []
    for n, (idx, r) in enumerate(targets, 1):
        prompt = PROMPT_TMPL.format(
            lang_name=LANG_NAME[r["language"]],
            word=r["word"], rarity=r["rarity"], power=r["power"],
            old_riddle=r["riddle"],
        )
        # Try up to 3 times; if validation rejects, append a stronger reminder
        # and retry — LLM stochasticity means same prompt can give different
        # output on a second attempt.
        new_riddle = None
        last_reason = None
        for retry in range(3):
            try:
                p = prompt
                if retry > 0:
                    p = prompt + (f"\n\nNOTE: previous attempt failed validation: "
                                  f"{last_reason}. Try again — the riddle MUST NOT "
                                  f"contain the substring \"{r['word']}\".")
                cand = call_claude(p)
            except Exception as e:
                print(f"  [{n:3d}/{len(targets)}] wid={idx} ERROR  {e}")
                last_reason = str(e)
                continue
            ok, reason = validate_new_riddle(cand, r)
            if ok:
                new_riddle = cand
                break
            last_reason = reason
        if new_riddle is None:
            print(f"  [{n:3d}/{len(targets)}] wid={idx} REJECT after 3 tries  {last_reason}")
            failed.append((idx, last_reason or "unknown"))
            continue
        data[idx] = {**r, "riddle": new_riddle}
        fixed.append(idx)
        print(f"  [{n:3d}/{len(targets)}] wid={idx} {r['language']}  '{r['word']}'  → {new_riddle[:60]}…")

    print(f"\nfixed: {len(fixed)} / failed: {len(failed)}")

    if fixed:
        # Backup, then write atomically
        backup = RIDDLES.with_suffix(".json.bak")
        shutil.copy(RIDDLES, backup)
        print(f"backed up original → {backup}")
        tmp = RIDDLES.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(RIDDLES)
        print(f"wrote {RIDDLES}")

    if failed:
        print(f"\nremaining failures (re-run to retry):")
        for idx, reason in failed:
            print(f"  wid={idx}: {reason}")


if __name__ == "__main__":
    main()
