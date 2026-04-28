"""Fusion LLM oracle — V2 rule framework.

Design philosophy (locked 2026-04-26):
  * Good pairings (T1 / T2) succeed almost always but pay a small multiplier.
  * Wild pairings (T4 / T5) succeed rarely but, when they do, pay big.
  * Mid pairings (T3) sit in the middle.

To keep the LLM honest, compatibility is NOT a free-form [0, 1] number.
The LLM picks ONE of 5 categorical tiers and a `tier_subscore ∈ [0, 1]`
indicating where in that tier it lands. Coordinator then derives the final
compatibility score deterministically from (tier, tier_subscore, pair_key).
This makes evaluations reproducible and auditable — the same pair always
maps to the same tier; only the subscore can drift slightly.

The multiplier and success-rate curves are smooth (no piecewise cliffs)
and parametric so future tuning is a constant tweak, not a logic rewrite.

  multiplier(c) = MULT_BASE + MULT_SPREAD × (1 - c) ** MULT_EXPONENT
  success(c)    = max(SUCCESS_FLOOR, sigmoid(SIG_K × (c - SIG_MID)))

Default values target the EV table in docs/fusion-rules-v2.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .db import DB

log = logging.getLogger("ardi.fusion")

LANG_REV = {0: "en", 1: "zh", 2: "ja", 3: "ko", 4: "fr", 5: "de"}
LANG_FWD = {v: k for k, v in LANG_REV.items()}

# Hard limits on LLM-suggested fields. The on-chain `power` field is uint16,
# so 65535 is the absolute ceiling — anything above and the fuse tx will fail
# at ABI encode time. We cap defensively here so the Coordinator never signs
# an authorization that the user can't redeem.
MAX_POWER_UINT16 = 65535
MAX_SUGGESTED_WORD_LEN = 32
# Forbidden chars in suggested_word: control chars and `||` (the `||` token is
# our pair_key delimiter — a word containing it would poison cache lookups).
_FORBIDDEN_WORD_CHARS = ("\n", "\r", "\t", "\x00")


# ============================================================================
# V2 tunable parameters (single source of truth — no piecewise constants)
# ============================================================================

# 5-tier compatibility rubric. LLM picks a tier; Coordinator maps to a final
# compat score in the tier's closed interval.
TIER_RANGES: dict[str, tuple[float, float]] = {
    "T1": (0.85, 0.99),  # Identical concept (cross-language same-meaning, strict synonyms)
    "T2": (0.65, 0.84),  # Strong relation   (same domain, natural pair / complement)
    "T3": (0.40, 0.64),  # Loose relation    (same broad category, narratively linkable)
    "T4": (0.15, 0.39),  # Weak association  (indirect / fantasy-bridge)
    "T5": (0.01, 0.14),  # Unrelated         (no semantic overlap)
}
VALID_TIERS = set(TIER_RANGES.keys())

# Jitter applied to (tier_lo + (tier_hi - tier_lo) * subscore) — keeps repeat
# calls of the same (tier, subscore) from clustering at identical floats.
# Bounded so it can't push the result out of the chosen tier.
TIER_JITTER_AMPLITUDE = 0.025

# Multiplier curve. Smooth, monotone, parametric.
#   multiplier(c) = MULT_BASE + MULT_SPREAD × (1 - c) ** MULT_EXPONENT
# v1.4 — third rebalance. Tester said v1.3 (1.05/1.30/2.0) made fusion
# feel pointless: at typical compat 0.5-0.7, mult was ~1.35-1.55, so
# winning a fuse barely beat just holding both originals. The PERMANENT
# stat consolidation (1 NFT carrying the boosted power) didn't feel
# worth the risk of losing the lower one.
#
# Goal: meaningful per-tier reward floor while keeping the asymmetric
# break-even property:
#   T5 (c~0.10)  : mult ~ 2.37  (lottery, +137% on win)
#   T4 (c~0.25)  : mult ~ 2.10  (high risk / very high reward)
#   T3 (c~0.50)  : mult ~ 1.74  (worth the gamble — clean +74%)
#   T2 (c~0.75)  : mult ~ 1.46  (safer, still solid +46%)
#   T1 (c~0.95)  : mult ~ 1.31  (sure-thing +31%)
#
# Switched to EXP=1.5 instead of 2.0: gentler curve, mid-range pairs
# don't fall off a cliff. BASE 1.30 ensures even soulmate-pairs get a
# meaningful permanent power boost instead of "barely above 1×".
MULT_BASE = 1.30
MULT_SPREAD = 1.25
MULT_EXPONENT = 1.5

# Success-rate curve. Sigmoid centered at SIG_MID, slope SIG_K. Floor caps
# the bottom so even T5 has decent odds (was 0.05; raised to 0.30 in v1.1).
#   success(c) = max(SUCCESS_FLOOR, 1 / (1 + exp(-SIG_K × (c - SIG_MID))))
# v1.1: midpoint shifted left (0.50 → 0.40), gentler slope (8 → 7), and a
# higher floor (0.05 → 0.30). Net: every fusion has at least 30% chance,
# T2/T3 hit ~67-92%, T1 still at the ~98% ceiling.
SIG_MID = 0.40
SIG_K = 7.0
SUCCESS_FLOOR = 0.30

# Output language policy:
#   "parent" = randomly pick from (lang_a, lang_b)  — strict, default
#   "free"   = LLM may pick any of the 6 supported langs
SUGGESTED_LANG_POLICY = "parent"

# Cache key version. Bump this whenever the LLM model, prompt structure, or
# tier rubric changes — the new version is namespaced into pair_key so old
# cache entries are simply ignored (cache miss → re-evaluate). This avoids
# permanent staleness from a single early hallucination.
CACHE_VERSION = "v2-tier-2026-04"


class FusionValidationError(ValueError):
    """LLM output failed structural validation. Raised before signing /
    caching so a hallucinated/malformed response cannot poison state."""


SYSTEM_PROMPT = """You are the FUSION ORACLE for the Ardi WorkNet inscription system.

Your task: given two words (possibly different languages), classify their
SEMANTIC COMPATIBILITY into one of 5 tiers, then propose a new word that
captures the fusion of the two.

# COMPATIBILITY TIER RUBRIC — pick exactly ONE

  T1  Identical / Strict synonyms          [score 0.85-0.99]
      The two words denote the same concept across languages, or are strict
      synonyms within one language.
      Examples: 文化 (zh) + 文化 (ja); fire (en) + 火 (zh); begin + commence
      WATCH OUT: false friends — same spelling, different meaning, NOT T1.
      e.g. "parole" en (release) vs fr (speech) → T4 at most.

  T2  Strong relation                       [score 0.65-0.84]
      Same domain, complementary or natural pair, classical opposites.
      Examples: fire + water; sun + moon; king + queen; doctor + 病院.

  T3  Loose relation                        [score 0.40-0.64]
      Same broad category (emotions, weather, technology) OR can be linked
      by a coherent short narrative.
      Examples: love + 友情; computer + 智能; thunder + 雨.

  T4  Weak association                      [score 0.15-0.39]
      Indirect link via culture / fantasy / context. Fanciful but not random.
      Examples: king + sword; dog + blockchain (memecoin lore); witch + wand.

  T5  Unrelated                             [score 0.01-0.14]
      No identifiable semantic overlap. Pure accidental pairing.
      Examples: bitcoin + 土豆; computer + 幸運; sun + 杀人犯.

# OUTPUT SCHEMA (single JSON object, no markdown, no preamble)

{
  "tier": "T1" | "T2" | "T3" | "T4" | "T5",     // required
  "tier_subscore": 0.0..1.0,                     // where in the tier you land (0 = low end, 1 = high end)
  "suggested_word": string ≤ 32 chars,           // the fusion product word
  "suggested_language": "en"|"zh"|"ja"|"ko"|"fr"|"de",   // MUST be one of the two PARENT languages
  "rationale": "one short sentence explaining the tier choice"
}

# RULES
- The OUTPUT WORD must be in one of the two parents' languages — pick whichever fits semantically best.
- Do NOT include the rationale or tier in the suggested_word.
- Do NOT use control characters or "||" in suggested_word.
- Be DECISIVE on the tier. If torn between two tiers, pick the lower one.
"""


@dataclass
class FusionResult:
    compatibility: float
    tier: str
    suggested_word: str
    suggested_language_id: int
    rationale: str
    success: bool
    new_power: int  # (powerA + powerB) × multiplier when success
    cached: bool


@dataclass
class FusionPotential:
    """Read-only quote view — what COULD happen, not what WILL happen.

    `quote` returns this; the caller sees the odds + risk surface but the
    actual dice roll is deferred to sign-time. Notably absent:
        - the suggested new word (preserves "what will I get?" suspense)
        - the success boolean (the roll hasn't happened yet)
    """
    compatibility: float
    tier: str
    rationale: str
    success_rate: float            # P(success) given current curves
    multiplier: float              # the (powerA + powerB) multiplier on success
    power_if_success: int          # deterministic outcome of the formula on success
    cached: bool


# ============================================================================
# V2 curves
# ============================================================================

def _multiplier(compatibility: float) -> float:
    """Smooth multiplier curve. High compat → low payout, low compat → high payout."""
    c = max(0.0, min(1.0, compatibility))
    return MULT_BASE + MULT_SPREAD * ((1.0 - c) ** MULT_EXPONENT)


def _success_rate(compatibility: float) -> float:
    """Sigmoid success curve floored at SUCCESS_FLOOR."""
    c = max(0.0, min(1.0, compatibility))
    main = 1.0 / (1.0 + math.exp(-SIG_K * (c - SIG_MID)))
    return max(SUCCESS_FLOOR, main)


def _pair_key(word_a: str, lang_a: int, word_b: str, lang_b: int) -> str:
    """Canonical pair key — lexicographically smaller word first.

    The CACHE_VERSION is prefixed so that bumping the LLM model or prompt
    structure invalidates old cache rows (they become unreachable). The
    `||` delimiter is consistent with the on-chain `keccak256(|| ...)` style
    and is forbidden inside `suggested_word` (validator catches that).
    """
    a = f"{lang_a}:{word_a}"
    b = f"{lang_b}:{word_b}"
    pair = f"{a}||{b}" if a <= b else f"{b}||{a}"
    return f"{CACHE_VERSION}||{pair}"


def _tier_to_compat(tier: str, tier_subscore: float, pair_key: str) -> float:
    """Map (tier, subscore, pair_key) → final compat score, deterministically.

    Within the tier's closed range, the subscore picks a base point and a
    pair_key-derived jitter (±TIER_JITTER_AMPLITUDE) prevents identical
    subscores from collapsing onto the same float — but the jitter is
    bounded so the result never escapes the chosen tier.
    """
    if tier not in TIER_RANGES:
        raise FusionValidationError(f"unknown tier: {tier!r}")
    s = max(0.0, min(1.0, float(tier_subscore)))
    lo, hi = TIER_RANGES[tier]
    base = lo + (hi - lo) * s
    # Deterministic jitter, ±TIER_JITTER_AMPLITUDE
    h = hashlib.sha256(pair_key.encode()).digest()
    jitter = (int.from_bytes(h[:4], "big") / 2**32 - 0.5) * 2 * TIER_JITTER_AMPLITUDE
    return max(lo, min(hi, base + jitter))


def _validate_llm_output(
    raw: dict,
    parent_langs: tuple[int, int] | None = None,
) -> tuple[str, float, str, int, str]:
    """Validate + normalize raw LLM JSON.

    Returns (tier, tier_subscore, suggested_word, lang_id, rationale).

    The new V2 schema requires the LLM to output a categorical tier + a
    subscore — NOT a free-form compatibility number. The final compat is
    derived deterministically from (tier, subscore, pair_key) by the caller.

    Backwards-compatible path: if the LLM (or a legacy cache row) provides
    a top-level `compatibility` float instead of a tier, we map it back to
    the corresponding tier. This lets existing fusion_cache rows survive
    a Coordinator upgrade without forcing a re-evaluation.

    Defense layers:
      1. tier ∈ {T1..T5} (or compat ∈ [0,1] for legacy rows)
      2. tier_subscore numeric in [0, 1]
      3. suggested_word: non-empty ≤ MAX_SUGGESTED_WORD_LEN, no `||`, no control chars
      4. suggested_language in {en,zh,ja,ko,fr,de} AND, when policy=="parent",
         must be one of the two parents' languages. NO silent fallback.
    """
    if not isinstance(raw, dict):
        raise FusionValidationError(f"output not a dict: {type(raw).__name__}")

    # ---- tier (or legacy compat → derived tier) -----------------------------
    tier_raw = raw.get("tier")
    if isinstance(tier_raw, str) and tier_raw in VALID_TIERS:
        tier = tier_raw
    else:
        # Legacy / compat-style input: derive tier from a [0,1] compatibility.
        legacy_comp = raw.get("compatibility")
        if not isinstance(legacy_comp, (int, float)) or isinstance(legacy_comp, bool):
            raise FusionValidationError(
                f"missing or invalid tier (got {tier_raw!r}); "
                f"no legacy 'compatibility' fallback either"
            )
        c = float(legacy_comp)
        if c < 0.0 or c > 1.0:
            raise FusionValidationError(f"legacy compatibility out of [0,1]: {c}")
        tier = _compat_to_tier(c)

    # tier_subscore: prefer explicit, otherwise infer from legacy compat
    sub_raw = raw.get("tier_subscore")
    if isinstance(sub_raw, (int, float)) and not isinstance(sub_raw, bool):
        subscore = float(sub_raw)
        if subscore < 0.0 or subscore > 1.0:
            raise FusionValidationError(f"tier_subscore out of [0,1]: {subscore}")
    elif "compatibility" in raw and tier_raw not in VALID_TIERS:
        # Legacy row — recover position within tier from the original compat
        c = float(raw["compatibility"])
        lo, hi = TIER_RANGES[tier]
        subscore = 0.0 if hi == lo else max(0.0, min(1.0, (c - lo) / (hi - lo)))
    else:
        # No subscore given — default to mid-tier
        subscore = 0.5

    # ---- suggested_word ----------------------------------------------------
    word = raw.get("suggested_word")
    if not isinstance(word, str):
        raise FusionValidationError(f"suggested_word not a string: {type(word).__name__}")
    if len(word) == 0:
        raise FusionValidationError("suggested_word empty")
    if len(word) > MAX_SUGGESTED_WORD_LEN:
        raise FusionValidationError(
            f"suggested_word too long ({len(word)} > {MAX_SUGGESTED_WORD_LEN})"
        )
    if "||" in word:
        raise FusionValidationError("suggested_word contains '||' (pair_key delimiter)")
    if any(c in word for c in _FORBIDDEN_WORD_CHARS):
        raise FusionValidationError("suggested_word contains control characters")

    # ---- suggested_language ------------------------------------------------
    lang_str = raw.get("suggested_language")
    lang_id = raw.get("suggested_language_id")
    if isinstance(lang_id, int) and 0 <= lang_id <= 5:
        pass
    elif isinstance(lang_str, str) and lang_str in LANG_FWD:
        lang_id = LANG_FWD[lang_str]
    else:
        raise FusionValidationError(
            f"suggested_language invalid: lang_str={lang_str!r}, lang_id={lang_id!r}"
        )

    # Policy: under "parent" mode, the new lang MUST be one of the parents'.
    if SUGGESTED_LANG_POLICY == "parent" and parent_langs is not None:
        if lang_id not in parent_langs:
            raise FusionValidationError(
                f"suggested_language {LANG_REV[lang_id]} not in parent languages "
                f"{[LANG_REV[l] for l in parent_langs]}"
            )

    # ---- rationale (truncated) ---------------------------------------------
    rationale = raw.get("rationale", "")
    if not isinstance(rationale, str):
        rationale = str(rationale)
    rationale = rationale[:512]

    return tier, subscore, word, int(lang_id), rationale


def _compat_to_tier(compat: float) -> str:
    """Inverse mapping for legacy / debug. Returns the tier whose range contains compat."""
    for tier, (lo, hi) in TIER_RANGES.items():
        if lo <= compat <= hi:
            return tier
    # Outside any range — clamp to nearest boundary tier
    return "T1" if compat > 0.99 else "T5"


def _capped_new_power(power_a: int, power_b: int, comp: float) -> int:
    """Compute new_power capped at MAX_POWER_UINT16. The on-chain newPower
    field is uint16, so anything above this would fail ABI encoding when
    the user tries to submit the fuse tx. Cap and log."""
    raw = int((power_a + power_b) * _multiplier(comp))
    if raw > MAX_POWER_UINT16:
        log.warning(
            f"new_power capped: raw={raw} → {MAX_POWER_UINT16} "
            f"(parents {power_a} + {power_b}, comp {comp:.2f})"
        )
        return MAX_POWER_UINT16
    return raw


class FusionOracle:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self._cache_dir = Path(cfg.fusion.cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def evaluate(
        self,
        word_a: str,
        lang_a: int,
        power_a: int,
        word_b: str,
        lang_b: int,
        power_b: int,
    ) -> FusionResult:
        """Evaluate a fusion pair. Always validates LLM output before caching
        or returning — a bad output is rejected (raises FusionValidationError)
        rather than silently accepted with a fallback.
        """
        key = _pair_key(word_a, lang_a, word_b, lang_b)
        parent_langs = (lang_a, lang_b)
        cached = self._read_cache(key)
        if cached:
            log.debug(f"fusion cache hit for {key}")
            # Cached rows are pre-validated — but defensively re-validate, as
            # an older Coordinator version may have written something the new
            # validator rejects (forward-compatibility safety).
            try:
                tier, subscore, new_word, new_lang, rationale = _validate_llm_output(
                    {
                        "tier": cached.get("tier"),
                        "tier_subscore": cached.get("tier_subscore"),
                        "compatibility": cached["compatibility"],  # legacy fallback
                        "suggested_word": cached["suggested_word"],
                        "suggested_language_id": cached["suggested_language_id"],
                        "rationale": cached["rationale"],
                    },
                    parent_langs=parent_langs,
                )
            except FusionValidationError as e:
                log.warning(f"stale invalid cache for {key}: {e} — refreshing")
                cached = None
            else:
                comp = _tier_to_compat(tier, subscore, key)
                success = self._roll_success(comp, key)
                new_power = _capped_new_power(power_a, power_b, comp) if success else 0
                return FusionResult(
                    compatibility=comp,
                    tier=tier,
                    suggested_word=new_word,
                    suggested_language_id=new_lang,
                    rationale=rationale,
                    success=success,
                    new_power=new_power,
                    cached=True,
                )

        # Cache miss — invoke LLM and validate BEFORE caching
        result_json = await self._invoke_llm(word_a, lang_a, word_b, lang_b)
        try:
            tier, subscore, new_word, new_lang, rationale = _validate_llm_output(
                result_json, parent_langs=parent_langs
            )
        except FusionValidationError as e:
            log.error(
                f"LLM produced invalid output for ({word_a}|{lang_a} + {word_b}|{lang_b}): {e}"
            )
            raise

        comp = _tier_to_compat(tier, subscore, key)
        self._write_cache(key, {
            "tier": tier,
            "tier_subscore": subscore,
            "compatibility": comp,  # also persist final compat for indexer/api
            "suggested_word": new_word,
            "suggested_language_id": new_lang,
            "rationale": rationale,
        })
        success = self._roll_success(comp, key)
        new_power = _capped_new_power(power_a, power_b, comp) if success else 0
        return FusionResult(
            compatibility=comp,
            tier=tier,
            suggested_word=new_word,
            suggested_language_id=new_lang,
            rationale=rationale,
            success=success,
            new_power=new_power,
            cached=False,
        )

    async def evaluate_potential(
        self,
        word_a: str, lang_a: int, power_a: int,
        word_b: str, lang_b: int, power_b: int,
    ) -> FusionPotential:
        """Read-only potential preview — runs the LLM compat evaluation and
        the deterministic compat→tier→multiplier math, but does NOT roll
        success. Caller (forge.quote) uses this to surface odds without
        leaking the eventual outcome.

        Internally re-uses `evaluate()`'s LLM + cache path; just discards
        the rolled success and trims the suggested_word from the response.
        """
        # Reuse evaluate's LLM call (which does cache the suggested word).
        # We then ignore success/new_word for the public-facing return.
        full = await self.evaluate(word_a, lang_a, power_a, word_b, lang_b, power_b)
        comp = full.compatibility
        sr = _success_rate(comp)
        mult = _multiplier(comp)
        return FusionPotential(
            compatibility=comp,
            tier=full.tier,
            rationale=full.rationale,
            success_rate=sr,
            multiplier=mult,
            power_if_success=_capped_new_power(power_a, power_b, comp),
            cached=full.cached,
        )

    def roll_outcome(
        self,
        word_a: str, lang_a: int, power_a: int,
        word_b: str, lang_b: int, power_b: int,
        suggested_word: str,
        suggested_language_id: int,
        compatibility: float,
        salt: bytes,
    ) -> tuple[bool, int]:
        """Sign-time dice roll. Uses a fresh, caller-provided `salt` (e.g.
        on-chain fusionNonce + holder + tokenIds) so the result is
        per-sign-attempt — not deterministic from (word_a, word_b) alone.

        Returns (success, new_power). new_power=0 on failure.
        """
        sr = _success_rate(compatibility)
        # Cryptographic random in [0, 1) — uses os.urandom under the hood.
        h = hashlib.sha256(salt + os.urandom(16)).digest()
        roll = int.from_bytes(h[:8], "big") / (1 << 64)
        success = roll < sr
        new_power = _capped_new_power(power_a, power_b, compatibility) if success else 0
        return success, new_power

    def _roll_success(self, compatibility: float, key: str) -> bool:
        """LEGACY: Deterministic success roll based on key hash + compatibility threshold.
        Uses a sha256-derived float so repeated calls with the same key give the
        same outcome (matches the on-chain expectation that fuse is reproducible)."""
        h = hashlib.sha256(key.encode()).digest()
        roll = int.from_bytes(h[:8], "big") / (1 << 64)
        return roll < _success_rate(compatibility)

    def _read_cache(self, key: str) -> dict[str, Any] | None:
        with self.db.conn() as c:
            row = c.execute(
                "SELECT compatibility, suggested_word, suggested_lang, rationale, "
                "tier, tier_subscore "
                "FROM fusion_cache WHERE pair_key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        return {
            "compatibility": row["compatibility"],
            "suggested_word": row["suggested_word"],
            "suggested_language_id": row["suggested_lang"],
            "rationale": row["rationale"],
            "tier": row["tier"],            # may be NULL on legacy rows
            "tier_subscore": row["tier_subscore"],
        }

    def _write_cache(self, key: str, result: dict[str, Any]):
        """Write a validated fusion result to cache. Caller must have already
        run `_validate_llm_output` so we can trust the shape here."""
        with self.db.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO fusion_cache "
                "(pair_key, compatibility, suggested_word, suggested_lang, "
                "rationale, cached_at, tier, tier_subscore) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    float(result["compatibility"]),
                    result["suggested_word"],
                    int(result["suggested_language_id"]),
                    result.get("rationale", ""),
                    int(time.time()),
                    result.get("tier"),
                    result.get("tier_subscore"),
                ),
            )

    async def _invoke_llm(self, word_a: str, lang_a: int, word_b: str, lang_b: int) -> dict[str, Any]:
        """Call the LLM oracle. Three providers in priority order:
          1. anthropic-api  — direct https://api.anthropic.com call (needs key)
          2. claude-cli     — `claude -p` subprocess (uses CLI's logged-in
                              session; no key required, works on testnet
                              when ANTHROPIC_API_KEY isn't set)
          3. anthropic      — legacy alias, falls back to (1) but tolerates
                              missing key by routing through claude-cli
        """
        provider = self.cfg.fusion.provider
        if provider not in ("anthropic", "anthropic-api", "claude-cli"):
            raise NotImplementedError(f"provider {provider} not supported")

        prompt = (
            f"Word A: {word_a} (language: {LANG_REV[lang_a]})\n"
            f"Word B: {word_b} (language: {LANG_REV[lang_b]})\n\n"
            "Output a single JSON object with keys: compatibility (float 0..1), "
            "suggested_word (string), suggested_language (one of en|zh|ja|ko|fr|de), "
            "rationale (one sentence)."
        )

        api_key = self.cfg.fusion.api_key or ""
        use_cli = provider == "claude-cli" or (provider in ("anthropic", "anthropic-api") and not api_key)

        if use_cli:
            text = await self._invoke_claude_cli(SYSTEM_PROMPT, prompt)
        else:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.cfg.fusion.model,
                        "max_tokens": 200,
                        "temperature": 0,
                        "system": SYSTEM_PROMPT,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"]

        # Strip markdown if any
        text = re.sub(r"```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```", "", text)
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise ValueError(f"no JSON object in LLM response: {text!r}")
        return json.loads(m.group(0))

    async def _invoke_claude_cli(self, system: str, user: str) -> str:
        """Invoke `claude -p` (Claude Code CLI) — uses the user's logged-in
        session, no API key needed. Combines system + user prompt into one
        message because `claude -p` doesn't expose a separate system slot.

        Resilience:
          - The Claude Code session occasionally returns the literal string
            "Execution error" instead of completing (rate limit, transient
            session issue, content moderation refusal). We retry up to 3
            times with brief backoff; if all fail, we raise a friendly
            error so the caller can surface a 'try again' message instead
            of a 500.
        """
        import asyncio
        combined = f"{system}\n\n---\n\n{user}\n\nReturn ONLY the JSON object, no preamble."
        last_text = ""
        for attempt in range(3):
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", "--output-format", "text",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(combined.encode("utf-8")),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                proc.kill()
                last_text = "<timeout>"
                if attempt < 2:
                    await asyncio.sleep(2.0 + attempt)
                    continue
                raise RuntimeError("claude -p fusion call timed out (3 attempts)")
            if proc.returncode != 0:
                last_text = stderr.decode()[:200]
                if attempt < 2:
                    await asyncio.sleep(2.0 + attempt)
                    continue
                raise RuntimeError(f"claude -p exit {proc.returncode}: {last_text}")
            text = stdout.decode("utf-8").strip()
            # Detect known transient refusal patterns. Claude Code returns
            # "Execution error" for a few different transient conditions —
            # rate limit on the user's session, momentary content filter
            # tripped, etc. Retry rather than fail the whole forge call.
            if (text == "Execution error"
                    or text == ""
                    or len(text) < 20):
                last_text = text
                if attempt < 2:
                    await asyncio.sleep(2.0 + attempt)
                    continue
                raise RuntimeError(
                    f"oracle returned no usable response after 3 attempts "
                    f"(last: {last_text!r}). The Claude Code session may be "
                    f"rate-limited; please try the fuse again in 30 seconds."
                )
            return text
        return last_text  # unreachable but keeps lint happy
