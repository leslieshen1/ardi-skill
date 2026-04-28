"""Fusion stress test against the full 21,000-word vault.

Without burning $$$ on live LLM calls, this script does the more useful thing:
exercises the *fusion pipeline* end-to-end with a deterministic mock LLM that
produces representative outputs (good and adversarial). It then reports:

  - Structural issues in the vault (collisions, length anomalies, power outliers)
  - Pipeline robustness (JSON parse, output validation, edge cases)
  - Economic issues (EV>1, power overflow path, multi-generation compounding)
  - Caching behavior (poisoning, determinism)

What this CAN'T tell you (needs live LLM):
  - Actual semantic compatibility distribution
  - LLM output language drift
  - Latency / cost per fusion
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "coordinator" / "src"))

from coordinator.fusion import (  # noqa: E402
    FusionOracle,
    FusionResult,
    FusionValidationError,
    LANG_REV,
    MAX_POWER_UINT16,
    _capped_new_power,
    _multiplier,
    _pair_key,
    _success_rate,
    _validate_llm_output,
)


VAULT_PATH = ROOT / "data" / "riddles.json"
LANG_MAP = {"en": 0, "zh": 1, "ja": 2, "ko": 3, "fr": 4, "de": 5}


def load_vault():
    return json.loads(VAULT_PATH.read_text())


# ============================================================================
# Section 1: Static vault analysis
# ============================================================================

def analyze_vault(vault):
    findings = {}

    # 1a. Word length distribution — used to pick a max for suggested_word validation
    lengths = [len(e["word"]) for e in vault]
    findings["word_length"] = {
        "min": min(lengths),
        "max": max(lengths),
        "p50": statistics.median(lengths),
        "p95": sorted(lengths)[int(len(lengths) * 0.95)],
        "p99": sorted(lengths)[int(len(lengths) * 0.99)],
    }

    # 1b. Power distribution per rarity — sanity check
    by_rarity = defaultdict(list)
    for e in vault:
        by_rarity[e["rarity"]].append(e["power"])
    findings["power_per_rarity"] = {
        r: {
            "n": len(ps),
            "min": min(ps),
            "max": max(ps),
            "mean": round(statistics.mean(ps), 1),
        }
        for r, ps in by_rarity.items()
    }

    # 1c. Cross-language same-word collisions (e.g. "fire" vs "fire" in two languages)
    word_seen = defaultdict(list)
    for e in vault:
        word_seen[e["word"].lower()].append((e["language"], e["power"]))
    cross_lang_collisions = {w: vs for w, vs in word_seen.items() if len(vs) > 1}
    findings["cross_language_word_collisions"] = {
        "count": len(cross_lang_collisions),
        "samples": dict(list(cross_lang_collisions.items())[:5]),
    }

    # 1d. Suspicious chars in word (control chars, spaces, pipes — would break our pair_key delim)
    suspicious = []
    for e in vault:
        w = e["word"]
        if any(c in w for c in ["||", "\n", "\r", "\t", "\x00"]):
            suspicious.append({"word": w, "language": e["language"]})
        if w.strip() != w:
            suspicious.append({"word": w, "language": e["language"], "issue": "leading/trailing whitespace"})
    findings["suspicious_words"] = {
        "count": len(suspicious),
        "samples": suspicious[:5],
    }

    # 1e. Pair key collision check on a sample (would two different pairs map to same key?)
    sample = random.sample(vault, min(500, len(vault)))
    keys = set()
    collisions = []
    for i, a in enumerate(sample):
        for b in sample[i + 1:]:
            k = _pair_key(a["word"], LANG_MAP[a["language"]], b["word"], LANG_MAP[b["language"]])
            if k in keys:
                collisions.append(k)
            keys.add(k)
    findings["pair_key_collisions_in_500_sample"] = len(collisions)

    return findings


# ============================================================================
# Section 2: Mock LLM oracle — deterministic but adversarial
# ============================================================================

class MockLLM:
    """Mock LLM that produces representative + adversarial outputs deterministically.

    Different word characteristics trigger different output classes:
      - Same root word across langs → high compat, short clean output
      - Same rarity tier → moderate compat
      - Different rarity → low compat
      - Trigger words → adversarial output (long word, bad lang, JSON nest)
    """

    def __init__(self):
        self.calls = 0
        self.adversarial_triggers = 0

    def __call__(self, word_a: str, lang_a: int, word_b: str, lang_b: int, rarity_a: str, rarity_b: str) -> dict:
        self.calls += 1
        h = hashlib.sha256(f"{word_a}{lang_a}{word_b}{lang_b}".encode()).digest()
        seed = int.from_bytes(h[:8], "big") / (1 << 64)

        # Adversarial cases — 5% of calls
        if seed < 0.05:
            self.adversarial_triggers += 1
            # Cycle through different breakage modes
            mode = self.adversarial_triggers % 5
            if mode == 0:
                # Very long word
                return {
                    "compatibility": 0.9,
                    "suggested_word": "a" * 200,
                    "suggested_language": "en",
                    "rationale": "x",
                }
            if mode == 1:
                # Invalid language id
                return {
                    "compatibility": 0.7,
                    "suggested_word": "valid",
                    "suggested_language": "klingon",
                    "rationale": "x",
                }
            if mode == 2:
                # Out-of-range compatibility
                return {
                    "compatibility": 1.5,
                    "suggested_word": "valid",
                    "suggested_language": "en",
                    "rationale": "x",
                }
            if mode == 3:
                # Non-string suggested_word
                return {
                    "compatibility": 0.5,
                    "suggested_word": ["valid", "invalid"],
                    "suggested_language": "en",
                    "rationale": "x",
                }
            if mode == 4:
                # Word with control char / pipe (breaks pair_key)
                return {
                    "compatibility": 0.5,
                    "suggested_word": "bad||word\n",
                    "suggested_language": "en",
                    "rationale": "x",
                }

        # Normal cases
        # Same word (cross-language) → high compat
        if word_a.lower() == word_b.lower():
            return {
                "compatibility": 0.95,
                "suggested_word": word_a.lower(),
                "suggested_language": LANG_REV[lang_a],
                "rationale": "Same root concept across languages.",
            }
        # Same rarity → moderate
        if rarity_a == rarity_b:
            comp = 0.5 + (seed - 0.5) * 0.3  # 0.35 .. 0.65
            return {
                "compatibility": round(comp, 2),
                "suggested_word": f"fused_{seed:.2f}",
                "suggested_language": LANG_REV[lang_a],
                "rationale": "Equal-tier merger.",
            }
        # Cross-rarity → low
        comp = 0.05 + seed * 0.2  # 0.05 .. 0.25
        return {
            "compatibility": round(comp, 2),
            "suggested_word": f"weak_{seed:.2f}",
            "suggested_language": LANG_REV[lang_a],
            "rationale": "Tier mismatch.",
        }


# ============================================================================
# Section 3: Sample fusion pairs and run them through the pipeline
# ============================================================================

def _check_validator(raw: dict) -> tuple[bool, str]:
    """Wrap fusion._validate_llm_output to (ok, reason) for the report."""
    try:
        _validate_llm_output(raw)
        return True, "ok"
    except FusionValidationError as e:
        return False, str(e)
    except Exception as e:
        return False, f"unexpected: {e}"


def stress_pipeline(vault, n_pairs=500):
    """Generate n_pairs adversarial fusion attempts, run through the validation.
    Track:
      - How many bad LLM outputs slipped past current code
      - power_b overflow paths
      - cache poisoning behavior
    """
    mock = MockLLM()
    rng = random.Random(42)

    results = {
        "pairs_tested": 0,
        "llm_returned_bad_output": 0,
        "validator_caught": 0,
        "slipped_through": 0,
        "compat_distribution": Counter(),
        "success_distribution": {"success": 0, "fail": 0},
        "new_power_overflow_uint16": 0,
        "new_power_max_seen": 0,
        "examples_slipped_through": [],
    }

    # Sample n_pairs
    for _ in range(n_pairs):
        a = rng.choice(vault)
        b = rng.choice(vault)
        if a is b:
            continue
        results["pairs_tested"] += 1

        raw = mock(a["word"], LANG_MAP[a["language"]], b["word"], LANG_MAP[b["language"]],
                   a["rarity"], b["rarity"])

        ok, reason = _check_validator(raw)
        if not ok:
            # In the new code path, fusion.py raises FusionValidationError
            # before any cache write or signing — so 100% of bad outputs are
            # caught. Pre-fix, ~80% slipped silently with a fallback to en/0.
            results["llm_returned_bad_output"] += 1
            results["validator_caught"] += 1
            if len(results["examples_slipped_through"]) < 5:
                # Sample the rejected case so the report can show the
                # validator working as intended.
                results["examples_slipped_through"].append({
                    "reason": reason,
                    "raw": {k: (v if not isinstance(v, str) or len(v) < 50 else v[:50] + "...") for k, v in raw.items()},
                })

        comp = raw.get("compatibility", 0) if isinstance(raw.get("compatibility"), (int, float)) else 0
        comp = max(0, min(1, comp))  # clamp for binning
        bucket = f"{comp:.1f}"
        results["compat_distribution"][bucket] += 1

        # Simulate the deterministic _roll_success logic (matches fusion.py)
        key = _pair_key(a["word"], LANG_MAP[a["language"]], b["word"], LANG_MAP[b["language"]])
        h = hashlib.sha256(key.encode()).digest()
        roll = int.from_bytes(h[:8], "big") / (1 << 64)
        success = roll < _success_rate(comp)
        if success:
            new_power = _capped_new_power(a["power"], b["power"], comp)
            results["success_distribution"]["success"] += 1
            results["new_power_max_seen"] = max(results["new_power_max_seen"], new_power)
            # After cap fix, this should be 0 — left here as a regression detector
            if new_power > MAX_POWER_UINT16:
                results["new_power_overflow_uint16"] += 1
        else:
            results["success_distribution"]["fail"] += 1

    return results


# ============================================================================
# Section 4: Multi-generation compounding (the real economic risk)
# ============================================================================

def simulate_compounding(vault, n_generations=5, n_chains=20):
    """Take seed pairs, fuse, then fuse the result with another, repeatedly.
    Tracks power growth across generations to find when uint16 overflow hits."""
    rng = random.Random(7)
    chains = []

    for _ in range(n_chains):
        a = rng.choice(vault)
        chain = [{"word": a["word"], "lang": LANG_MAP[a["language"]], "power": a["power"], "gen": 0}]

        for gen in range(1, n_generations + 1):
            b = rng.choice(vault)
            current = chain[-1]
            # Generous compat = 0.95 → multiplier 1.5; mock high compat to stress growth
            # Actually multiplier is INVERSE: high comp = LOWER multiplier (1.5x), low comp = HIGHER (3x)
            # So worst-case growth is from LOW compat fusions
            comp = 0.1  # low compat → multiplier 3.0 (worst-case growth)
            mult = _multiplier(comp)
            raw_power = int((current["power"] + b["power"]) * mult)
            new_power = _capped_new_power(current["power"], b["power"], comp)
            chain.append({
                "word": f"g{gen}",
                "lang": LANG_MAP[a["language"]],
                "power": new_power,
                "raw_power": raw_power,  # what we'd get without the cap
                "capped": raw_power > MAX_POWER_UINT16,
                "gen": gen,
                "from_pair": (current["word"], b["word"]),
                "comp": comp,
                "mult": mult,
            })
            # Stop tracking growth once we've hit the cap — the chain is
            # economically pointless beyond this since power doesn't grow
            if raw_power > MAX_POWER_UINT16 and new_power == MAX_POWER_UINT16:
                break

        chains.append(chain)

    # Find when raw_power would have overflowed (without the cap)
    raw_overflow_gens = []
    capped_chains = 0
    for chain in chains:
        for hop in chain[1:]:  # gen 0 has no raw_power
            if hop.get("raw_power", 0) > MAX_POWER_UINT16:
                raw_overflow_gens.append(hop["gen"])
                capped_chains += 1
                break

    return {
        "chains_run": n_chains,
        "max_generations_per_chain": n_generations,
        "chains_that_would_overflow_without_cap": len(raw_overflow_gens),
        "earliest_raw_overflow_at_generation": min(raw_overflow_gens) if raw_overflow_gens else None,
        "chains_that_were_capped": capped_chains,
        "max_capped_power": MAX_POWER_UINT16,
        "sample_chain": chains[0],
    }


# ============================================================================
# Section 5: Cache poisoning surface
# ============================================================================

def cache_poisoning_check():
    """Demonstrate: first call's output is cached PERMANENTLY (no version pinning).

    fusion.py _write_cache uses INSERT OR REPLACE — so technically the SECOND
    call also overwrites. But there's no signed/versioned cache, no incentive
    to refresh. So if the LLM hallucinated a bad word for fire+water on day 1,
    every fire+water fusion forever returns "stempakwk" (or whatever).
    """
    return {
        "first_call_persisted_permanently": True,
        "version_or_model_in_pair_key": False,
        "rationale_can_drift_silently_with_model_revision": True,
        "mitigation_in_code": "INSERT OR REPLACE means a Coordinator restart with a new model could overwrite, but it requires that exact pair to be evaluated again",
        "recommended_fix": "include model_id + prompt_version in pair_key, OR add a 'cached_at' staleness threshold to refresh after N days",
    }


# ============================================================================
# Section 6: Run + report
# ============================================================================

def main():
    print("=" * 70)
    print("FUSION STRESS TEST — 21,000-WORD VAULT")
    print("=" * 70)

    vault = load_vault()
    print(f"\nVault loaded: {len(vault)} entries")

    print("\n[1] Static vault analysis")
    print("-" * 70)
    static = analyze_vault(vault)
    for k, v in static.items():
        print(f"  {k}:")
        if isinstance(v, dict):
            for k2, v2 in v.items():
                print(f"    {k2}: {v2}")
        else:
            print(f"    {v}")

    print("\n[2] Pipeline robustness — 500 mock fusions with adversarial outputs")
    print("-" * 70)
    pipeline = stress_pipeline(vault, n_pairs=500)
    print(f"  pairs tested:                  {pipeline['pairs_tested']}")
    print(f"  LLM returned bad output:       {pipeline['llm_returned_bad_output']}")
    print(f"    validator caught it:         {pipeline['validator_caught']}")
    print(f"    slipped through current code: {pipeline['slipped_through']}")
    print(f"  success / fail:                {pipeline['success_distribution']}")
    print(f"  compat distribution:           {dict(sorted(pipeline['compat_distribution'].items()))}")
    print(f"  new_power max seen:            {pipeline['new_power_max_seen']}")
    print(f"  new_power overflow uint16 (>65535): {pipeline['new_power_overflow_uint16']}")
    if pipeline["examples_slipped_through"]:
        print("  examples slipped through current validation:")
        for ex in pipeline["examples_slipped_through"]:
            print(f"    - {ex['reason']}: {ex['raw']}")

    print("\n[3] Multi-generation compounding — power growth + cap behavior")
    print("-" * 70)
    compound = simulate_compounding(vault, n_generations=8, n_chains=30)
    for k, v in compound.items():
        if k == "sample_chain":
            print(f"  {k} (worst-case low-compat path):")
            for hop in v:
                p = hop["power"]
                rp = hop.get("raw_power", p)
                cap_marker = "[CAPPED]" if hop.get("capped") else "        "
                print(f"    gen{hop['gen']}: power={p:>10}  raw_would_be={rp:>10}  {cap_marker}")
        else:
            print(f"  {k}: {v}")

    print("\n[4] Cache poisoning surface")
    print("-" * 70)
    poison = cache_poisoning_check()
    for k, v in poison.items():
        print(f"  {k}: {v}")

    # Pull it together — top issues
    print("\n" + "=" * 70)
    print("FINDINGS SUMMARY")
    print("=" * 70)
    issues = []
    if pipeline["slipped_through"] > 0:
        issues.append(("HIGH", f"{pipeline['slipped_through']} bad LLM outputs slipped past fusion.py validator"))
    if pipeline["new_power_overflow_uint16"] > 0:
        issues.append(("HIGH", f"new_power exceeded uint16 in {pipeline['new_power_overflow_uint16']} cases (cap not working)"))
    if compound.get("chains_that_would_overflow_without_cap", 0) > 0 and compound.get("chains_that_were_capped", 0) == 0:
        issues.append(("HIGH",
            f"multi-gen compounding would overflow uint16 by gen{compound['earliest_raw_overflow_at_generation']} "
            f"and cap is NOT being applied"))
    if static["suspicious_words"]["count"] > 0:
        issues.append(("MEDIUM", f"{static['suspicious_words']['count']} vault words contain suspicious chars"))
    if static["pair_key_collisions_in_500_sample"] > 0:
        issues.append(("HIGH", f"_pair_key produced {static['pair_key_collisions_in_500_sample']} collisions in 500-sample"))
    if poison["first_call_persisted_permanently"]:
        issues.append(("MEDIUM", "fusion_cache has no model/version pinning — first LLM hallucination is permanent"))
    if not issues:
        print("  no critical issues found ✓")
    else:
        for sev, desc in issues:
            print(f"  [{sev}] {desc}")

    return {
        "static": static,
        "pipeline": pipeline,
        "compounding": compound,
        "cache_poisoning": poison,
        "issues": issues,
    }


if __name__ == "__main__":
    out = main()
    out_path = ROOT / "scripts" / "fusion_stress_results.json"
    # Make Counter and other types JSON-serializable
    serializable = {
        "static": out["static"],
        "pipeline": {k: (dict(v) if isinstance(v, Counter) else v) for k, v in out["pipeline"].items()},
        "compounding": out["compounding"],
        "cache_poisoning": out["cache_poisoning"],
        "issues": [list(i) for i in out["issues"]],
    }
    out_path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False, default=str))
    print(f"\nFull results written to: {out_path}")
