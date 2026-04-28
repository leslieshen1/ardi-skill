"""Sample 100 diverse fusion pairs from the 21K vault for LLM evaluation.

Selection design (deterministic, seed=42):
  - 20 same-word cross-language    (e.g. "nft" en + "nft" zh)   — expect HIGH compat
  - 20 related concepts same-lang  (e.g. "fire" + "water" en)   — expect MED-HIGH
  - 20 unrelated cross-lang        (e.g. "bitcoin" en + 土豆 zh) — expect LOW
  - 20 same-rarity any-lang random                              — expect MED
  - 20 cross-rarity any-lang random                              — expect LOW-MED

Output: scripts/fusion_sample_100.json — list of {pair_id, a, b, category}
which then gets fed to a real LLM (or me-as-LLM) for evaluation.
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
VAULT = json.loads((ROOT / "data" / "riddles.json").read_text())

# Conceptual buckets for the "related concepts" sampler.
# Just hand-picked English keywords — sampler finds vault entries matching.
RELATED_GROUPS = [
    ["fire", "water", "earth", "wind", "ice", "stone", "metal", "wood"],  # elements
    ["bitcoin", "ethereum", "coin", "token", "money", "wallet", "bank"],  # crypto / finance
    ["car", "truck", "wheel", "road", "driver", "tire", "engine"],          # vehicles
    ["bread", "rice", "noodle", "soup", "tea", "coffee", "cake"],            # food
    ["king", "queen", "prince", "knight", "castle", "crown", "sword"],       # royalty
    ["computer", "phone", "screen", "mouse", "keyboard", "internet"],         # tech
    ["dog", "cat", "fish", "bird", "horse", "tiger", "rabbit"],               # animals
    ["love", "hate", "joy", "fear", "hope", "dream", "trust"],                # emotions
    ["sun", "moon", "star", "sky", "cloud", "rain", "snow"],                  # weather
    ["hand", "foot", "head", "eye", "ear", "mouth", "heart"],                 # body
]


def find_word(vault, word: str) -> dict | None:
    for e in vault:
        if e["word"].lower() == word.lower():
            return e
    return None


def main():
    rng = random.Random(42)
    pairs = []

    # --- 20 same-word cross-language --------------------------------------
    by_word = defaultdict(list)
    for e in VAULT:
        by_word[e["word"].lower()].append(e)
    cross_roots = [(w, ents) for w, ents in by_word.items() if len(ents) >= 2]
    rng.shuffle(cross_roots)
    for word, ents in cross_roots[:20]:
        pairs.append({
            "category": "same_word_cross_lang",
            "expected": "HIGH",
            "a": ents[0],
            "b": ents[1],
        })

    # --- 20 related concepts same-language --------------------------------
    n_related = 0
    for group in RELATED_GROUPS:
        if n_related >= 20:
            break
        # find vault entries matching keywords in same language
        for lang in ["en"]:
            found = [find_word(VAULT, w) for w in group]
            found = [e for e in found if e and e["language"] == lang]
            if len(found) < 2:
                continue
            # take 2-3 unordered pairs from this group
            for i in range(len(found)):
                for j in range(i + 1, len(found)):
                    if n_related >= 20:
                        break
                    pairs.append({
                        "category": "related_same_lang",
                        "expected": "MED-HIGH",
                        "a": found[i],
                        "b": found[j],
                    })
                    n_related += 1
                if n_related >= 20:
                    break
            if n_related >= 20:
                break

    # --- 20 unrelated cross-language --------------------------------------
    # bitcoin (en) + 土豆 (zh-potato), king (en) + 寿司 (zh/ja-sushi), etc.
    unrelated_targets = [
        ("bitcoin", "en"), ("car", "en"), ("love", "en"), ("dog", "en"),
        ("king", "en"), ("computer", "en"), ("sun", "en"), ("fire", "en"),
        ("water", "en"), ("bread", "en"),
    ]
    for word, lang_a in unrelated_targets:
        a = find_word(VAULT, word)
        if not a:
            continue
        # pick a random foreign-language word that isn't conceptually related
        foreign = [e for e in VAULT if e["language"] != lang_a and e["language"] != "en"]
        for _ in range(2):
            b = rng.choice(foreign)
            if b["word"].lower() != a["word"].lower():
                pairs.append({
                    "category": "unrelated_cross_lang",
                    "expected": "LOW",
                    "a": a,
                    "b": b,
                })

    # --- 20 same-rarity any-lang random -----------------------------------
    by_rarity = defaultdict(list)
    for e in VAULT:
        by_rarity[e["rarity"]].append(e)
    for _ in range(20):
        r = rng.choice(["common", "uncommon", "rare", "legendary"])
        pool = by_rarity[r]
        a = rng.choice(pool)
        b = rng.choice(pool)
        if a is b:
            continue
        pairs.append({
            "category": "same_rarity_random",
            "expected": "MED",
            "a": a,
            "b": b,
        })

    # --- 20 cross-rarity random -------------------------------------------
    rarities = ["common", "uncommon", "rare", "legendary"]
    for _ in range(20):
        r1 = rng.choice(rarities)
        r2 = rng.choice([r for r in rarities if r != r1])
        a = rng.choice(by_rarity[r1])
        b = rng.choice(by_rarity[r2])
        pairs.append({
            "category": "cross_rarity_random",
            "expected": "LOW-MED",
            "a": a,
            "b": b,
        })

    # Number them
    for i, p in enumerate(pairs):
        p["pair_id"] = i + 1

    out = ROOT / "scripts" / "fusion_sample_100.json"
    out.write_text(json.dumps(pairs, indent=2, ensure_ascii=False))
    print(f"Sampled {len(pairs)} pairs → {out}")
    by_cat = defaultdict(int)
    for p in pairs:
        by_cat[p["category"]] += 1
    for k, v in by_cat.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
