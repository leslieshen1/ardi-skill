#!/usr/bin/env python3
"""regen_native_riddles.py — replace English riddles in data/riddles.json
with native-language riddles for all non-en entries.

Uses `claude -p` (Claude Code CLI) as the LLM backend — relies on the user's
logged-in session, no API key needed.

Strategy:
  1. Load riddles.json (21K entries).
  2. For each entry where language != "en" AND riddle_lang_done is False,
     spawn `claude -p` to generate a native-language riddle.
  3. Validate output (length, no target-word leak).
  4. On leak/short/empty: retry once with stronger prompt.
  5. Checkpoint every CHECKPOINT_EVERY entries to .partial.json so a
     mid-run crash can resume.
  6. After all done, atomically rename .partial.json → riddles.json
     (backing up the original to riddles.en_backup.json).

Concurrency: ThreadPoolExecutor with N workers each running a subprocess.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

LANG_FULL = {
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
}

RARITY_HINT = {
    "common": "common (clear, fairly direct hints)",
    "uncommon": "uncommon (oblique, requires a moment of thought)",
    "rare": "rare (metaphorical, with a single clear path to the answer)",
    "legendary": "legendary (poetic, requires lateral thinking but solvable)",
}

# Localized prompt to make the LLM stay in the target language. Riddle
# instructions in EACH target language reduce English bleed-through.
LOCALIZED_INSTR = {
    "zh": "请用中文写一个谜面（2-4句），谜底就是给出的词。",
    "ja": "日本語で謎を書いてください（2〜4文）。答えは指定された語そのものです。",
    "ko": "한국어로 수수께끼를 작성하세요 (2~4문장). 정답은 주어진 단어입니다.",
    "fr": "Rédigez une devinette en français (2-4 phrases). La réponse est exactement le mot donné.",
    "de": "Schreibe ein Rätsel auf Deutsch (2-4 Sätze). Die Lösung ist genau das angegebene Wort.",
}


def build_prompt(word: str, language: str, rarity: str, attempt: int) -> str:
    lang_full = LANG_FULL[language]
    rarity_hint = RARITY_HINT.get(rarity, RARITY_HINT["common"])
    localized = LOCALIZED_INSTR.get(language, "")

    extra = ""
    if attempt > 0:
        extra = (
            f"\n\nIMPORTANT: the riddle MUST NOT contain the word '{word}', "
            f"any inflection, romanization, or trivial synonym. "
            f"If a previous attempt included it, this attempt must avoid it strictly."
        )

    return (
        f"{localized}\n"
        f"Target word: {word}\n"
        f"Language: {lang_full}\n"
        f"Difficulty: {rarity_hint}\n\n"
        f"Rules:\n"
        f"- Write the riddle in {lang_full} ONLY. No English. "
        f"No pinyin / no romaji / no transliteration.\n"
        f"- 2-4 sentences. Evocative, descriptive, not a definition.\n"
        f"- The riddle MUST NOT literally contain the target word "
        f"'{word}', any inflection of it, or its romanized form.\n"
        f"- Output ONLY the riddle text. No labels, no quotes, no preamble, "
        f"no explanation, no English meta commentary.\n"
        f"- Do not start with 「The」 or any English filler.{extra}"
    )


def call_claude(prompt: str, timeout: int = 150) -> str:
    """Run `claude -p` with the given prompt on stdin. Returns stdout text."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr[:200]}")
        return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude -p timed out")


def _has_word_boundary(text: str, word: str) -> bool:
    """Check `word` appears as a whole token in `text`. Whole-token semantics:
      - Latin scripts (en/fr/de): word-boundary regex (e.g. 'fin' won't
        match inside 'final').
      - CJK (zh/ja/ko): substring match — these scripts have no spaces,
        so a substring is the closest analog to "the word literally appears".
    """
    import re
    # Heuristic: if the word contains any ASCII letters, treat as Latin.
    if any('a' <= c.lower() <= 'z' for c in word):
        # Word-boundary regex; case-insensitive.
        pattern = r'\b' + re.escape(word) + r'\b'
        return re.search(pattern, text, re.IGNORECASE) is not None
    # CJK / accented-only / kana / hangul / etc. — substring is right.
    return word in text


def validate(riddle: str, word: str) -> tuple[bool, str]:
    """Check the generated riddle is acceptable.

    Returns (ok, reason). Reasons: 'too_short', 'too_long', 'leaks_word'.
    Empty reason on success.
    """
    if len(riddle) < 15:
        return False, "too_short"
    if len(riddle) > 800:
        return False, "too_long"
    # Whole-token leak: 'fin' in 'final' is OK; '愛' in '恋愛' is a leak
    # (the target morpheme is literally present).
    if len(word) >= 2 and _has_word_boundary(riddle, word):
        return False, "leaks_word"
    return True, ""


def generate_one(entry: dict, max_attempts: int = 2) -> tuple[int, str | None, str]:
    """Generate a native riddle for one entry. Returns (id, riddle_or_None, status).
    Status is 'ok', 'leaks_word', 'too_short', etc."""
    word = entry["word"]
    lang = entry["language"]
    rarity = entry.get("rarity", "common")

    last_riddle = ""
    last_status = "unknown"
    for attempt in range(max_attempts):
        try:
            prompt = build_prompt(word, lang, rarity, attempt)
            riddle = call_claude(prompt)
            ok, reason = validate(riddle, word)
            if ok:
                return entry["_word_id"], riddle, "ok"
            last_riddle = riddle
            last_status = reason
        except Exception as e:
            last_status = f"error:{str(e)[:60]}"
    # Couldn't generate clean — return best-effort with status flag
    return entry["_word_id"], last_riddle if last_riddle else None, last_status


# ---------------------------------------------------------------- batch mode

def build_batch_prompt(entries: list[dict]) -> str:
    """One prompt → N riddles. All entries must share the same language so
    the localized instruction is consistent. Returns the LLM prompt."""
    lang = entries[0]["language"]
    lang_full = LANG_FULL[lang]
    localized = LOCALIZED_INSTR.get(lang, "")

    # Build a JSON-shaped input list. Use simple array; LLMs handle this well.
    items_json = json.dumps(
        [
            {"wordId": e["_word_id"], "word": e["word"], "rarity": e.get("rarity", "common")}
            for e in entries
        ],
        ensure_ascii=False,
    )

    return (
        f"{localized}\n\n"
        f"You are writing riddles in {lang_full}. Below is a JSON array of "
        f"{len(entries)} entries — for each, write a riddle in {lang_full} "
        f"whose answer is EXACTLY the given word.\n\n"
        f"Input:\n{items_json}\n\n"
        f"Rules per riddle:\n"
        f"- Write in {lang_full} ONLY (no English / pinyin / romaji / transliteration).\n"
        f"- 2-4 sentences. Evocative, descriptive, not a definition.\n"
        f"- The riddle MUST NOT contain the target word, any inflection, or "
        f"its romanized form.\n"
        f"- Difficulty by rarity: common = clear hints; uncommon = oblique; "
        f"rare = metaphorical; legendary = poetic, lateral.\n\n"
        f"Output STRICTLY: a JSON array of length {len(entries)}, where each "
        f"object has exactly two keys — `wordId` (integer, matching input) "
        f"and `riddle` (string, in {lang_full}). No prose, no markdown, no "
        f"code fences, no commentary. Output the JSON only.\n"
    )


def parse_batch_response(text: str) -> dict[int, str]:
    """Extract wordId → riddle map from the LLM response. Tolerant to markdown
    code-fences and stray whitespace; if the response is not valid JSON,
    falls back to extracting the first JSON array via regex."""
    raw = text.strip()
    # Strip markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        arr = json.loads(raw)
    except Exception:
        # Try to find a JSON array
        m = re.search(r"\[\s*\{[\s\S]*\}\s*\]", raw)
        if not m:
            raise RuntimeError(f"no JSON array in response: {raw[:200]!r}")
        arr = json.loads(m.group(0))
    if not isinstance(arr, list):
        raise RuntimeError(f"expected JSON array, got {type(arr).__name__}")
    out: dict[int, str] = {}
    for obj in arr:
        if not isinstance(obj, dict):
            continue
        wid = obj.get("wordId")
        riddle = obj.get("riddle")
        if isinstance(wid, int) and isinstance(riddle, str) and riddle.strip():
            out[wid] = riddle.strip()
    return out


def generate_batch(entries: list[dict], max_attempts: int = 2) -> list[tuple[int, str | None, str]]:
    """Generate N riddles in one LLM call. Returns list of (word_id, riddle, status).
    Per-entry validation runs after parsing; failures get retried in a smaller
    individual-mode pass to avoid wasting the whole batch on one bad apple."""
    if not entries:
        return []
    last_err = "unknown"
    parsed: dict[int, str] = {}
    for attempt in range(max_attempts):
        try:
            prompt = build_batch_prompt(entries)
            text = call_claude(prompt, timeout=240)  # batches are slower
            parsed = parse_batch_response(text)
            if parsed:
                break
            last_err = "empty_parse"
        except Exception as e:
            last_err = f"error:{str(e)[:60]}"

    results: list[tuple[int, str | None, str]] = []
    retry_singles: list[dict] = []
    for e in entries:
        wid = e["_word_id"]
        riddle = parsed.get(wid)
        if not riddle:
            # Missing from response — schedule a single-mode retry
            retry_singles.append(e)
            continue
        ok, reason = validate(riddle, e["word"])
        if ok:
            results.append((wid, riddle, "ok"))
        else:
            # Validation failure — retry as single (stronger anti-leak prompt)
            retry_singles.append(e)

    # Per-entry retries for stragglers (covers missing + leaks_word + etc.)
    for e in retry_singles:
        results.append(generate_one(e, max_attempts=2))
    return results


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/riddles.json", help="path to source riddles.json")
    ap.add_argument("--output", default="data/riddles.json", help="path to write final result")
    ap.add_argument("--partial", default="data/riddles.partial.json", help="checkpoint file")
    ap.add_argument("--backup", default="data/riddles.en_backup.json", help="backup of the original English-only riddles")
    ap.add_argument("--workers", type=int, default=15, help="concurrent claude -p subprocesses")
    ap.add_argument("--checkpoint-every", type=int, default=50)
    ap.add_argument("--limit", type=int, default=0, help="if >0, regen only this many (debug)")
    ap.add_argument("--only-langs", default="", help="comma-list of langs to regen, e.g. zh,ja. empty=all non-en")
    ap.add_argument("--resume", action="store_true", help="resume from .partial if present")
    ap.add_argument(
        "--batch-size", type=int, default=1,
        help="riddles per LLM call. >1 amortizes per-call overhead and "
             "rate-limit hits; 8-12 is a good range. Each batch is a "
             "single language so the prompt stays consistent.",
    )
    args = ap.parse_args()

    base = Path(__file__).parent.parent
    in_path = base / args.input if not Path(args.input).is_absolute() else Path(args.input)
    out_path = base / args.output if not Path(args.output).is_absolute() else Path(args.output)
    partial_path = base / args.partial if not Path(args.partial).is_absolute() else Path(args.partial)
    backup_path = base / args.backup if not Path(args.backup).is_absolute() else Path(args.backup)

    if not in_path.exists():
        sys.exit(f"input not found: {in_path}")

    print(f"loading {in_path}", flush=True)
    data = json.loads(in_path.read_text())
    print(f"  {len(data)} entries", flush=True)
    # Inject _word_id == array index (entries don't carry an explicit id field).
    for i, e in enumerate(data):
        e["_word_id"] = i

    only_langs = set(s.strip() for s in args.only_langs.split(",") if s.strip())
    targets = []
    for entry in data:
        if entry["language"] == "en":
            continue
        if only_langs and entry["language"] not in only_langs:
            continue
        targets.append(entry)
    print(f"  {len(targets)} non-en target entries", flush=True)

    # Resume support: if .partial exists, load it as the working state.
    completed_ids: dict[int, dict] = {}  # id → entry-with-native-riddle
    if args.resume and partial_path.exists():
        print(f"resuming from {partial_path}", flush=True)
        partial = json.loads(partial_path.read_text())
        for e in partial:
            if e.get("riddle_lang_done"):
                completed_ids[e["_word_id"]] = e
        print(f"  {len(completed_ids)} already done", flush=True)

    # Make the original copy if we don't have one yet.
    if not backup_path.exists():
        print(f"backing up original to {backup_path}", flush=True)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(in_path, backup_path)

    # Filter to actually-pending
    pending = [e for e in targets if e["_word_id"] not in completed_ids]
    if args.limit > 0:
        pending = pending[: args.limit]
    print(f"  {len(pending)} pending after resume / limit", flush=True)

    if not pending:
        print("nothing to do.", flush=True)
        return

    lock = Lock()
    state_by_id = {e["_word_id"]: dict(e) for e in data}
    # Apply already-done from resume
    for cid, ce in completed_ids.items():
        state_by_id[cid] = ce

    counter = {"ok": 0, "fail": 0, "started": time.time()}

    def write_checkpoint():
        all_entries = [state_by_id[e["_word_id"]] for e in data]
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = partial_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(all_entries, ensure_ascii=False, indent=2))
        tmp.replace(partial_path)

    print(f"running with {args.workers} workers, batch_size={args.batch_size}...", flush=True)
    futures = []
    completed_count = 0

    def apply_result(eid: int, riddle: str | None, status: str):
        """Update state_by_id and counters for one finished entry. Caller
        holds the lock."""
        nonlocal completed_count
        completed_count += 1
        entry = state_by_id[eid]
        if riddle and status == "ok":
            entry["riddle"] = riddle
            entry["riddle_lang_done"] = True
            entry.pop("regen_status", None)
            counter["ok"] += 1
        elif riddle:
            entry["riddle"] = riddle
            entry["riddle_lang_done"] = False
            entry["regen_status"] = status
            counter["fail"] += 1
        else:
            entry["riddle_lang_done"] = False
            entry["regen_status"] = status
            counter["fail"] += 1

        if completed_count % args.checkpoint_every == 0 or completed_count == len(pending):
            write_checkpoint()
            elapsed = time.time() - counter["started"]
            rate = completed_count / max(elapsed, 1)
            eta = (len(pending) - completed_count) / max(rate, 0.001)
            print(
                f"[{completed_count}/{len(pending)}] ok={counter['ok']} fail={counter['fail']} "
                f"rate={rate:.1f}/s eta={eta/60:.1f}min "
                f"sample({entry['language']}/{entry['word']!r}): "
                f"{(entry.get('riddle') or '')[:60]}",
                flush=True,
            )

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        if args.batch_size <= 1:
            # Original per-entry mode — one LLM call per riddle
            for entry in pending:
                futures.append(ex.submit(generate_one, entry))
            for fut in as_completed(futures):
                try:
                    eid, riddle, status = fut.result()
                except Exception as e:
                    print(f"worker crashed: {e}", flush=True)
                    continue
                with lock:
                    apply_result(eid, riddle, status)
        else:
            # Batch mode — group entries by language, send N at a time.
            # Each batch is one LLM call → multiple riddles parsed back.
            by_lang: dict[str, list[dict]] = {}
            for e in pending:
                by_lang.setdefault(e["language"], []).append(e)
            for lang, lang_entries in by_lang.items():
                for i in range(0, len(lang_entries), args.batch_size):
                    chunk = lang_entries[i : i + args.batch_size]
                    futures.append(ex.submit(generate_batch, chunk))
            for fut in as_completed(futures):
                try:
                    results = fut.result()
                except Exception as e:
                    print(f"batch crashed: {e}", flush=True)
                    continue
                with lock:
                    for eid, riddle, status in results:
                        apply_result(eid, riddle, status)

    # Final atomic write — strip the synthetic _word_id before persisting.
    print("writing final output...", flush=True)
    final_entries = []
    for e in data:
        out_e = dict(state_by_id[e["_word_id"]])
        out_e.pop("_word_id", None)
        final_entries.append(out_e)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".final.tmp")
    tmp.write_text(json.dumps(final_entries, ensure_ascii=False, indent=2))
    tmp.replace(out_path)
    print(f"wrote {out_path}", flush=True)
    print(f"summary: ok={counter['ok']} fail={counter['fail']}", flush=True)


if __name__ == "__main__":
    main()
