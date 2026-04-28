"""Full fusion traversal of the 21,000-word vault.

Modes:
  1. Linear      — each word fuses with the next (21k fusions, baseline)
  2. Cross-lang  — every cross-language same-root collision pair
  3. Stratified  — uniform sample over (lang_a × rarity_a × lang_b × rarity_b) cells
  4. Compound    — N chains × K generations to characterize power growth

Uses the SAME deterministic mock LLM as fusion.py's pipeline:
  - same word → comp 0.95
  - same rarity → comp 0.35..0.65
  - cross rarity → comp 0.05..0.25
  - 5% adversarial outputs (caught by validator)

Then runs every fusion through the production fusion.py code path
(_validate_llm_output + _capped_new_power) so the validator + cap fixes
are exercised at full scale.

Output: scripts/FUSION_TRAVERSAL_REPORT.md
"""
from __future__ import annotations

import hashlib
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "coordinator" / "src"))

from coordinator.fusion import (  # noqa: E402
    FusionValidationError,
    LANG_REV,
    MAX_POWER_UINT16,
    _capped_new_power,
    _multiplier,
    _pair_key,
    _success_rate,
    _tier_to_compat,
    _validate_llm_output,
)


VAULT_PATH = ROOT / "data" / "riddles.json"
LANG_MAP = {"en": 0, "zh": 1, "ja": 2, "ko": 3, "fr": 4, "de": 5}


# ============================================================================
# Mock LLM (deterministic, hash-seeded)
# ============================================================================

def mock_llm(word_a: str, lang_a: int, power_a: int, rarity_a: str,
             word_b: str, lang_b: int, power_b: int, rarity_b: str) -> dict:
    h = hashlib.sha256(f"{word_a}|{lang_a}|{word_b}|{lang_b}".encode()).digest()
    seed = int.from_bytes(h[:8], "big") / (1 << 64)

    # 5% adversarial outputs — exercise validator
    if seed < 0.05:
        # Vary by sub-bucket: bad lang, bad compat, bad word, etc.
        sub = int(seed * 1e9) % 5
        if sub == 0:
            return {"compatibility": 0.5, "suggested_word": "x" * 50,
                    "suggested_language": "en", "rationale": "long"}
        if sub == 1:
            return {"compatibility": 1.5, "suggested_word": "ok",
                    "suggested_language": "en", "rationale": "out of range"}
        if sub == 2:
            return {"compatibility": 0.5, "suggested_word": "ok",
                    "suggested_language": "klingon", "rationale": "bad lang"}
        if sub == 3:
            return {"compatibility": 0.5, "suggested_word": "bad||word",
                    "suggested_language": "en", "rationale": "delim"}
        return {"compatibility": "0.5", "suggested_word": "ok",
                "suggested_language": "en", "rationale": "compat as string"}

    # Normal path
    if word_a.lower() == word_b.lower():
        comp = 0.95
        word = word_a.lower()
    elif rarity_a == rarity_b:
        comp = round(0.35 + seed * 0.3, 2)
        word = f"f{int(seed * 10000):04x}"
    else:
        comp = round(0.05 + seed * 0.2, 2)
        word = f"w{int(seed * 10000):04x}"

    return {
        "compatibility": comp,
        "suggested_word": word,
        "suggested_language": LANG_REV[lang_a],
        "rationale": "mock",
    }


# ============================================================================
# Single fusion through full pipeline
# ============================================================================

def _roll_success(key: str, comp: float) -> bool:
    h = hashlib.sha256(key.encode()).digest()
    roll = int.from_bytes(h[:8], "big") / (1 << 64)
    return roll < _success_rate(comp)


def fuse(a: dict, b: dict) -> dict:
    """Run a fusion pair through the production validation + cap pipeline.
    Returns a record dict. `validator_rejected=True` when LLM output is malformed.

    Mock LLM still emits the V1 free-form `compatibility` shape — the V2
    validator's backwards-compat path maps that into a tier internally.
    """
    raw = mock_llm(
        a["word"], LANG_MAP[a["language"]], a["power"], a["rarity"],
        b["word"], LANG_MAP[b["language"]], b["power"], b["rarity"],
    )

    try:
        # V2 validator returns (tier, subscore, word, lang_id, rationale).
        # No parent_langs constraint here — mock outputs use 'en' regardless.
        tier, sub, new_word, new_lang, _ = _validate_llm_output(raw)
    except FusionValidationError as e:
        return {
            "validator_rejected": True,
            "reason": str(e),
        }

    key = _pair_key(a["word"], LANG_MAP[a["language"]], b["word"], LANG_MAP[b["language"]])
    comp = _tier_to_compat(tier, sub, key)
    success = _roll_success(key, comp)
    new_power = _capped_new_power(a["power"], b["power"], comp) if success else 0
    return {
        "validator_rejected": False,
        "compatibility": comp,
        "tier": tier,
        "success": success,
        "new_word": new_word,
        "new_lang": new_lang,
        "new_power": new_power,
        "raw_power": int((a["power"] + b["power"]) * _multiplier(comp)) if success else 0,
        "capped": new_power == MAX_POWER_UINT16 and success,
    }


# ============================================================================
# Mode 1: Linear traversal — each word fuses with its neighbor
# ============================================================================

def linear_traversal(vault):
    """word[i] + word[i+1] for all i. 20999 fusions."""
    print(f"  Linear traversal: {len(vault) - 1} fusions...")
    t0 = time.time()
    stats = {
        "total": 0,
        "validator_rejected": 0,
        "success": 0,
        "fail": 0,
        "compat_buckets": Counter(),
        "power_distribution": Counter(),
        "raw_powers": [],
        "capped_count": 0,
        "cumulative_power_created": 0,
        "by_lang_pair_success": defaultdict(lambda: [0, 0]),  # [success, fail]
        "by_rarity_pair_success": defaultdict(lambda: [0, 0]),
    }

    for i in range(len(vault) - 1):
        r = fuse(vault[i], vault[i + 1])
        stats["total"] += 1
        if r["validator_rejected"]:
            stats["validator_rejected"] += 1
            continue

        c = r["compatibility"]
        stats["compat_buckets"][f"{c:.1f}"] += 1
        lang_pair = tuple(sorted([vault[i]["language"], vault[i + 1]["language"]]))
        rarity_pair = tuple(sorted([vault[i]["rarity"], vault[i + 1]["rarity"]]))

        if r["success"]:
            stats["success"] += 1
            stats["cumulative_power_created"] += r["new_power"]
            stats["raw_powers"].append(r["raw_power"])
            if r["capped"]:
                stats["capped_count"] += 1
            stats["by_lang_pair_success"][lang_pair][0] += 1
            stats["by_rarity_pair_success"][rarity_pair][0] += 1
            bucket = "0-100" if r["new_power"] < 100 else \
                     "100-500" if r["new_power"] < 500 else \
                     "500-1000" if r["new_power"] < 1000 else "1000+"
            stats["power_distribution"][bucket] += 1
        else:
            stats["fail"] += 1
            stats["by_lang_pair_success"][lang_pair][1] += 1
            stats["by_rarity_pair_success"][rarity_pair][1] += 1

    stats["elapsed_s"] = round(time.time() - t0, 2)
    return stats


# ============================================================================
# Mode 2: Cross-language same-root traversal
# ============================================================================

def cross_lang_traversal(vault):
    """Find every word that appears in 2+ languages, fuse all such pairs."""
    by_word = defaultdict(list)
    for e in vault:
        by_word[e["word"].lower()].append(e)

    cross_lang_pairs = []
    for word, entries in by_word.items():
        if len(entries) < 2:
            continue
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                cross_lang_pairs.append((entries[i], entries[j]))

    print(f"  Cross-lang traversal: {len(cross_lang_pairs)} pairs across {sum(1 for v in by_word.values() if len(v) > 1)} shared roots...")
    if not cross_lang_pairs:
        return {"total": 0, "note": "no cross-language collisions"}

    t0 = time.time()
    stats = {
        "total": 0,
        "success": 0,
        "fail": 0,
        "compats": [],
        "validator_rejected": 0,
    }
    for a, b in cross_lang_pairs:
        r = fuse(a, b)
        stats["total"] += 1
        if r["validator_rejected"]:
            stats["validator_rejected"] += 1
            continue
        stats["compats"].append(r["compatibility"])
        if r["success"]:
            stats["success"] += 1
        else:
            stats["fail"] += 1

    if stats["compats"]:
        stats["compat_min"] = min(stats["compats"])
        stats["compat_max"] = max(stats["compats"])
        stats["compat_mean"] = round(statistics.mean(stats["compats"]), 3)
    del stats["compats"]
    stats["elapsed_s"] = round(time.time() - t0, 2)
    return stats


# ============================================================================
# Mode 3: Stratified by (lang × rarity) cells
# ============================================================================

def stratified_traversal(vault, samples_per_cell=5):
    """For each (lang_a × rarity_a × lang_b × rarity_b) cell, sample N pairs."""
    import random
    rng = random.Random(42)

    by_lr = defaultdict(list)
    for e in vault:
        by_lr[(e["language"], e["rarity"])].append(e)

    cells = list(by_lr.keys())
    print(f"  Stratified: {len(cells) ** 2 // 2} cells × ~{samples_per_cell} samples...")

    t0 = time.time()
    stats = {
        "total": 0,
        "validator_rejected": 0,
        "success": 0,
        "fail": 0,
        "by_cell": {},
    }

    seen_cells = set()
    for c1 in cells:
        for c2 in cells:
            cell_key = tuple(sorted([c1, c2]))
            if cell_key in seen_cells:
                continue
            seen_cells.add(cell_key)

            cell_results = {"success": 0, "fail": 0, "validator_rejected": 0}
            pool_a = by_lr[c1]
            pool_b = by_lr[c2]
            if not pool_a or not pool_b:
                continue
            for _ in range(samples_per_cell):
                a = rng.choice(pool_a)
                b = rng.choice(pool_b)
                if a is b:
                    continue
                r = fuse(a, b)
                stats["total"] += 1
                if r["validator_rejected"]:
                    cell_results["validator_rejected"] += 1
                    stats["validator_rejected"] += 1
                    continue
                if r["success"]:
                    cell_results["success"] += 1
                    stats["success"] += 1
                else:
                    cell_results["fail"] += 1
                    stats["fail"] += 1
            stats["by_cell"][f"{c1[0]}/{c1[1]} × {c2[0]}/{c2[1]}"] = cell_results

    stats["elapsed_s"] = round(time.time() - t0, 2)
    # Don't dump 600+ cells in the JSON
    stats["cell_count"] = len(stats["by_cell"])
    sample_cells = list(stats["by_cell"].items())[:5]
    stats["sample_cells"] = sample_cells
    del stats["by_cell"]
    return stats


# ============================================================================
# Mode 4: Multi-generation compound chains
# ============================================================================

def compound_traversal(vault, n_chains=200, n_generations=12):
    """Each chain: pick start word, fuse with random partner, fuse result with random partner, ...
    Tracks power growth + cap behavior across many independent chains."""
    import random
    rng = random.Random(7)

    print(f"  Compound: {n_chains} chains × up to {n_generations} generations...")
    t0 = time.time()
    chains = []
    overflow_gens = []  # the gen at which each chain first hit cap (raw > 65535)
    final_powers = []

    for chain_idx in range(n_chains):
        a = rng.choice(vault)
        cur = {"word": a["word"], "language": a["language"], "rarity": a["rarity"], "power": a["power"]}
        capped_at = None

        for gen in range(1, n_generations + 1):
            # worst-case partner: low rarity (mismatched, multiplier 3.0) - actually random for realism
            b = rng.choice(vault)
            r = fuse(cur, b)
            if r["validator_rejected"] or not r["success"]:
                break  # chain dies on validator reject or fusion fail
            if r["raw_power"] > MAX_POWER_UINT16 and capped_at is None:
                capped_at = gen
            cur = {
                "word": r["new_word"],
                "language": LANG_REV[r["new_lang"]],
                # Synthesize rarity from new_power (rough)
                "rarity": "legendary" if r["new_power"] > 70 else
                          "rare" if r["new_power"] > 50 else
                          "uncommon" if r["new_power"] > 30 else "common",
                "power": r["new_power"],
            }

        if capped_at is not None:
            overflow_gens.append(capped_at)
        final_powers.append(cur["power"])
        chains.append({"final_power": cur["power"], "capped_at_gen": capped_at})

    elapsed = round(time.time() - t0, 2)
    return {
        "chains_run": n_chains,
        "max_generations": n_generations,
        "final_power_min": min(final_powers),
        "final_power_max": max(final_powers),
        "final_power_mean": round(statistics.mean(final_powers), 1),
        "final_power_p50": int(statistics.median(final_powers)),
        "chains_hit_cap": len(overflow_gens),
        "earliest_cap_gen": min(overflow_gens) if overflow_gens else None,
        "median_cap_gen": int(statistics.median(overflow_gens)) if overflow_gens else None,
        "elapsed_s": elapsed,
    }


# ============================================================================
# Main
# ============================================================================

def main():
    print("=" * 70)
    print("FUSION FULL TRAVERSAL — 21,000-WORD VAULT")
    print("=" * 70)

    vault = json.loads(VAULT_PATH.read_text())
    print(f"\nVault: {len(vault)} entries\n")

    print("[1/4] Linear (each word fuses with neighbor)")
    print("-" * 70)
    linear = linear_traversal(vault)
    print(f"   {linear['total']} fusions in {linear['elapsed_s']}s")
    print(f"   validator_rejected: {linear['validator_rejected']}")
    print(f"   success / fail: {linear['success']} / {linear['fail']} "
          f"({100 * linear['success'] / max(1, linear['success'] + linear['fail']):.1f}%)")
    print(f"   compat distribution: {dict(sorted(linear['compat_buckets'].items()))}")
    print(f"   power distribution: {dict(linear['power_distribution'])}")
    print(f"   capped at uint16 (success): {linear['capped_count']}")
    print(f"   cumulative power created: {linear['cumulative_power_created']:,}")

    # Top language pairs by success rate
    lang_rates = []
    for pair, (s, f) in linear["by_lang_pair_success"].items():
        if s + f >= 50:
            lang_rates.append((pair, s, f, s / (s + f)))
    lang_rates.sort(key=lambda x: -x[3])
    print(f"\n   Top language pair success rates (≥50 samples):")
    for pair, s, f, rate in lang_rates[:5]:
        print(f"     {pair}: {s}/{s+f} = {rate*100:.1f}%")

    print("\n[2/4] Cross-language same-root pairs")
    print("-" * 70)
    crosslang = cross_lang_traversal(vault)
    for k, v in crosslang.items():
        print(f"   {k}: {v}")

    print("\n[3/4] Stratified by (language × rarity) cells")
    print("-" * 70)
    stratified = stratified_traversal(vault, samples_per_cell=5)
    print(f"   {stratified['total']} fusions across {stratified['cell_count']} cells")
    print(f"   validator_rejected: {stratified['validator_rejected']}")
    print(f"   success / fail: {stratified['success']} / {stratified['fail']}")
    print(f"   sample cells:")
    for cell, r in stratified["sample_cells"]:
        print(f"     {cell}: {r}")

    print("\n[4/4] Multi-generation compound chains")
    print("-" * 70)
    compound = compound_traversal(vault, n_chains=200, n_generations=12)
    for k, v in compound.items():
        print(f"   {k}: {v}")

    # Aggregate
    print("\n" + "=" * 70)
    print("FINDINGS")
    print("=" * 70)
    issues = []
    total_fusions = linear["total"] + crosslang.get("total", 0) + stratified["total"]
    total_rejected = (
        linear["validator_rejected"]
        + crosslang.get("validator_rejected", 0)
        + stratified["validator_rejected"]
    )
    print(f"  Total fusions run: {total_fusions:,}")
    print(f"  Total validator rejects: {total_rejected:,} "
          f"({100 * total_rejected / max(1, total_fusions):.2f}%)")

    # Sanity: validator must catch all 5% adversarial outputs
    expected_rejects = int(0.05 * total_fusions)
    if total_rejected < expected_rejects * 0.7:
        issues.append(("HIGH", f"validator caught {total_rejected} but expected ~{expected_rejects} (≈5%) — some adversarial outputs slipped"))

    # Cap-hit rate sanity (Linear: shouldn't be too high; if very common, design intent question)
    if linear["capped_count"] > 0:
        issues.append(("INFO", f"linear traversal hit uint16 cap {linear['capped_count']} times — these are pairs where (a+b)*mult > 65535 even at gen 0 (e.g. high-power + low-compat)"))

    # Compound: verify cap absolutely prevents overflow (no >65535 finals)
    if compound["final_power_max"] > MAX_POWER_UINT16:
        issues.append(("HIGH", f"compound traversal final_power_max = {compound['final_power_max']} > 65535 — cap not working!"))
    else:
        print(f"  ✓ compound traversal: max final power {compound['final_power_max']} ≤ uint16 cap")

    if issues:
        print()
        for sev, desc in issues:
            print(f"  [{sev}] {desc}")
    else:
        print(f"  No critical issues. Pipeline behaves correctly at 21K scale ✓")

    # Persist results
    def normalize(v):
        if isinstance(v, (Counter, defaultdict)):
            return {str(k): normalize(vv) for k, vv in v.items()}
        if isinstance(v, dict):
            return {str(k): normalize(vv) for k, vv in v.items()}
        if isinstance(v, (list, tuple)):
            return [normalize(x) for x in v]
        return v

    out = {
        "vault_size": len(vault),
        "linear": {k: normalize(v) for k, v in linear.items() if k != "raw_powers"},
        "cross_language": normalize(crosslang),
        "stratified": normalize(stratified),
        "compound": normalize(compound),
        "total_fusions": total_fusions,
        "total_validator_rejects": total_rejected,
        "issues": [list(i) for i in issues],
    }
    out_path = ROOT / "scripts" / "fusion_traversal_results.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nResults: {out_path}")

    return out


if __name__ == "__main__":
    main()
