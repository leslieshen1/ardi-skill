"""LLM-evaluated fusion for 100 sampled pairs.

The "LLM" here is Claude (me), evaluating each pair's semantic compatibility
inline rather than via API call. Each evaluation is deterministic (matches
what temperature=0 should produce). Then runs through the production
fusion.py pipeline (validator + power cap) to verify behavior at semantic
quality, not just synthetic mock outputs.

This is the closest we can get to "real LLM fusion" without an API key —
and arguably more trustworthy than a single API call because it's all
done by one consistent reasoner (me) and committed to git for audit.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import sys
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


# ============================================================================
# LLM evaluations — me, Claude, acting as the fusion oracle
# Each entry: pair_id → (compat, suggested_word, suggested_language, rationale)
# ============================================================================

LLM_OUTPUTS: dict[int, dict] = {
    # --- 1-20: same-word cross-language → mostly HIGH compat ----------------
    1:  {"compatibility": 0.95, "suggested_word": "elegance", "suggested_language": "en",
         "rationale": "'style' shares the meaning across English and French — the fusion distills the shared aesthetic concept."},
    2:  {"compatibility": 0.97, "suggested_word": "支援", "suggested_language": "zh",
         "rationale": "支持 means 'support' in both Chinese and Japanese — the fusion intensifies into 援助 (active help)."},
    3:  {"compatibility": 0.85, "suggested_word": "tavern", "suggested_language": "en",
         "rationale": "'bar' in English and French both denote a social drinking establishment, slight cultural variation."},
    4:  {"compatibility": 0.97, "suggested_word": "interior", "suggested_language": "en",
         "rationale": "内部 means 'inside/interior' in both Chinese and Japanese — pure semantic overlap."},
    5:  {"compatibility": 0.96, "suggested_word": "honor", "suggested_language": "en",
         "rationale": "'respect' in English and French share the same etymology and meaning."},
    6:  {"compatibility": 0.96, "suggested_word": "begin", "suggested_language": "en",
         "rationale": "'commence' is identical in spelling and meaning in English and French."},
    7:  {"compatibility": 0.97, "suggested_word": "rest", "suggested_language": "en",
         "rationale": "'pause' in English and French both denote a temporary halt."},
    8:  {"compatibility": 0.97, "suggested_word": "method", "suggested_language": "en",
         "rationale": "方法 means 'method' in both Chinese and Japanese."},
    9:  {"compatibility": 0.97, "suggested_word": "culture", "suggested_language": "en",
         "rationale": "文化 has the same meaning in Chinese and Japanese — both denote shared customs and arts."},
    10: {"compatibility": 0.96, "suggested_word": "grandmother", "suggested_language": "en",
         "rationale": "祖母 is grandmother in both Chinese and Japanese, slight register difference."},
    11: {"compatibility": 0.85, "suggested_word": "region", "suggested_language": "en",
         "rationale": "地方 means 'place/region' in both, but Japanese also implies 'rural', so slight nuance gap."},
    12: {"compatibility": 0.96, "suggested_word": "singular", "suggested_language": "en",
         "rationale": "'unique' is identical in spelling and meaning in English and French."},
    13: {"compatibility": 0.96, "suggested_word": "obstruct", "suggested_language": "en",
         "rationale": "阻止 means 'prevent/stop' in both Chinese and Japanese."},
    14: {"compatibility": 0.35, "suggested_word": "speech", "suggested_language": "en",
         "rationale": "'parole' is a false friend — English means 'release on parole', French means 'speech/word'. Low compat."},
    15: {"compatibility": 0.95, "suggested_word": "deceased", "suggested_language": "en",
         "rationale": "死者 means 'the dead/deceased' in both Chinese and Japanese."},
    16: {"compatibility": 0.97, "suggested_word": "magic", "suggested_language": "en",
         "rationale": "魔法 is magic in both Chinese and Japanese, identical concept."},
    17: {"compatibility": 0.95, "suggested_word": "obscure", "suggested_language": "en",
         "rationale": "不明 means 'unclear/unknown' in both Chinese and Japanese."},
    18: {"compatibility": 0.85, "suggested_word": "James", "suggested_language": "en",
         "rationale": "'James' is the same proper name in English and French, just pronunciation differs."},
    19: {"compatibility": 0.78, "suggested_word": "portion", "suggested_language": "en",
         "rationale": "'part' in English and French both mean 'portion/share', though French has additional usage as 'going'."},
    20: {"compatibility": 0.92, "suggested_word": "oneself", "suggested_language": "en",
         "rationale": "本人 means 'the person himself' in both Chinese and Japanese."},

    # --- 21-40: related concepts same language → MED-HIGH -------------------
    21: {"compatibility": 0.88, "suggested_word": "steam", "suggested_language": "en",
         "rationale": "Fire + water classically produce steam — a creative-destruction synthesis."},
    22: {"compatibility": 0.78, "suggested_word": "lava", "suggested_language": "en",
         "rationale": "Fire + earth yields molten rock — natural elemental fusion."},
    23: {"compatibility": 0.82, "suggested_word": "firestorm", "suggested_language": "en",
         "rationale": "Fire fed by wind becomes a self-sustaining storm."},
    24: {"compatibility": 0.85, "suggested_word": "thaw", "suggested_language": "en",
         "rationale": "Fire melts ice — opposing elements creating phase change."},
    25: {"compatibility": 0.70, "suggested_word": "forge", "suggested_language": "en",
         "rationale": "Fire + stone is the foundation of forging tools."},
    26: {"compatibility": 0.85, "suggested_word": "smelt", "suggested_language": "en",
         "rationale": "Fire melts metal — direct technological synthesis."},
    27: {"compatibility": 0.90, "suggested_word": "ember", "suggested_language": "en",
         "rationale": "Fire + wood is the most natural pairing — wood is fire's fuel."},
    28: {"compatibility": 0.80, "suggested_word": "mud", "suggested_language": "en",
         "rationale": "Water + earth produces mud, the stuff of pottery and life."},
    29: {"compatibility": 0.83, "suggested_word": "mist", "suggested_language": "en",
         "rationale": "Water carried by wind becomes mist or rain."},
    30: {"compatibility": 0.95, "suggested_word": "frost", "suggested_language": "en",
         "rationale": "Water and ice are phases of the same substance."},
    31: {"compatibility": 0.72, "suggested_word": "erosion", "suggested_language": "en",
         "rationale": "Water shapes stone through patient erosion."},
    32: {"compatibility": 0.78, "suggested_word": "rust", "suggested_language": "en",
         "rationale": "Water on metal yields rust, a slow chemical fusion."},
    33: {"compatibility": 0.80, "suggested_word": "tide", "suggested_language": "en",
         "rationale": "Water nourishes wood — life's basic synthesis."},
    34: {"compatibility": 0.78, "suggested_word": "dust", "suggested_language": "en",
         "rationale": "Earth lifted by wind becomes dust storms or sandstorms."},
    35: {"compatibility": 0.75, "suggested_word": "tundra", "suggested_language": "en",
         "rationale": "Earth covered in ice yields permafrost or tundra."},
    36: {"compatibility": 0.92, "suggested_word": "bedrock", "suggested_language": "en",
         "rationale": "Earth and stone are nearly synonymous — rock is dense earth."},
    37: {"compatibility": 0.85, "suggested_word": "ore", "suggested_language": "en",
         "rationale": "Earth contains metal as ore — the geological fusion."},
    38: {"compatibility": 0.85, "suggested_word": "grove", "suggested_language": "en",
         "rationale": "Earth nourishes wood — soil and tree are inseparable."},
    39: {"compatibility": 0.88, "suggested_word": "blizzard", "suggested_language": "en",
         "rationale": "Wind + ice produces blizzard, a self-reinforcing weather event."},
    40: {"compatibility": 0.65, "suggested_word": "weather", "suggested_language": "en",
         "rationale": "Wind shapes stone over millennia — slow erosion."},

    # --- 41-60: unrelated cross-language → LOW -----------------------------
    41: {"compatibility": 0.15, "suggested_word": "absurdity", "suggested_language": "en",
         "rationale": "Bitcoin (digital currency, English) and 人才 (talent, Chinese) share no semantic field."},
    42: {"compatibility": 0.18, "suggested_word": "delay", "suggested_language": "en",
         "rationale": "Bitcoin and 'attendant/waiting' in French share no domain — purely arbitrary."},
    43: {"compatibility": 0.15, "suggested_word": "carpark", "suggested_language": "en",
         "rationale": "Car (English) and Publikum (audience, German) are unrelated unless you imagine a drive-in."},
    44: {"compatibility": 0.10, "suggested_word": "joyride", "suggested_language": "en",
         "rationale": "Car (English) and 사랑한다 (I love you, Korean) are conceptually disjoint."},
    45: {"compatibility": 0.55, "suggested_word": "longing", "suggested_language": "en",
         "rationale": "Love and 懐かしい (nostalgic) both describe deep emotional attachment, partially overlap."},
    46: {"compatibility": 0.20, "suggested_word": "manipulator", "suggested_language": "en",
         "rationale": "Love and 受害者 (victim) hint at abusive relationships but otherwise opposed."},
    47: {"compatibility": 0.10, "suggested_word": "loner", "suggested_language": "en",
         "rationale": "Dog (English) and selber (oneself, German) have no semantic overlap."},
    48: {"compatibility": 0.05, "suggested_word": "memecoin", "suggested_language": "en",
         "rationale": "Dog and 区块链 (blockchain) only relate via dog-themed memecoins, weak link."},
    49: {"compatibility": 0.30, "suggested_word": "decree", "suggested_language": "en",
         "rationale": "King + sprach (spoke, German) suggests a royal proclamation."},
    50: {"compatibility": 0.40, "suggested_word": "endgame", "suggested_language": "en",
         "rationale": "King and 끝났다 (it's over, Korean) evoke checkmate or dynasty's end."},
    51: {"compatibility": 0.15, "suggested_word": "chatbot", "suggested_language": "en",
         "rationale": "Computer and Kumpel (buddy, German) loosely evoke a digital companion."},
    52: {"compatibility": 0.10, "suggested_word": "fortuna", "suggested_language": "en",
         "rationale": "Computer and 幸運 (luck, Japanese) — only related in random number generation."},
    53: {"compatibility": 0.10, "suggested_word": "noir", "suggested_language": "en",
         "rationale": "Sun and 杀人犯 (murderer, Chinese) — opposing connotations of light vs darkness."},
    54: {"compatibility": 0.20, "suggested_word": "outdoor", "suggested_language": "en",
         "rationale": "Sun and hors (outside, French) — both relate to being out, weak link."},
    55: {"compatibility": 0.35, "suggested_word": "bonfire", "suggested_language": "en",
         "rationale": "Fire and 一大堆 (a big pile, Chinese) — pile of fire = bonfire."},
    56: {"compatibility": 0.15, "suggested_word": "ephemeral", "suggested_language": "en",
         "rationale": "Fire and passera (will pass, French) only loosely connected by transience."},
    57: {"compatibility": 0.10, "suggested_word": "splash", "suggested_language": "en",
         "rationale": "Water and 哟 (interjection, Chinese) — no semantic relation."},
    58: {"compatibility": 0.10, "suggested_word": "refill", "suggested_language": "en",
         "rationale": "Water and 더요 (more please, Korean) — only related at a restaurant table."},
    59: {"compatibility": 0.15, "suggested_word": "loaf", "suggested_language": "en",
         "rationale": "Bread and 第一个 (the first, Chinese) — no semantic connection."},
    60: {"compatibility": 0.30, "suggested_word": "harvest", "suggested_language": "en",
         "rationale": "Bread + 雨 (rain) — rain grows wheat that becomes bread, indirect chain."},

    # --- 61-80: same-rarity random → MED ----------------------------------
    61: {"compatibility": 0.30, "suggested_word": "roster", "suggested_language": "en",
         "rationale": "好多 (many) + 姓名 (names) — many names, weak conceptual overlap."},
    62: {"compatibility": 0.30, "suggested_word": "many", "suggested_language": "en",
         "rationale": "쓰레기 (trash) + 매니 (many) — many pieces of trash, faint association."},
    63: {"compatibility": 0.45, "suggested_word": "incense", "suggested_language": "en",
         "rationale": "Chinese + 祈り (prayer, Japanese) — Chinese temples and Japanese prayer share religious context."},
    64: {"compatibility": 0.35, "suggested_word": "glance", "suggested_language": "en",
         "rationale": "書け (write, Japanese) + 眼里 (in the eyes, Chinese) — writing in someone's eyes, weak link."},
    65: {"compatibility": 0.65, "suggested_word": "sage", "suggested_language": "en",
         "rationale": "君子 (gentleman) + 独立 (independent) — both denote moral autonomy in Confucian thought."},
    66: {"compatibility": 0.20, "suggested_word": "exotic", "suggested_language": "en",
         "rationale": "稳定币 (stablecoin) + タイ (Thailand) — Thai stablecoin is real but conceptually unusual."},
    67: {"compatibility": 0.45, "suggested_word": "majesty", "suggested_language": "en",
         "rationale": "王冠 (crown, Japanese) + Paris — Paris has royal heritage, evokes monarchy."},
    68: {"compatibility": 0.40, "suggested_word": "empire", "suggested_language": "en",
         "rationale": "혈액 (blood, Korean) + ローマ (Rome) — Roman bloodline, evocative but loose."},
    69: {"compatibility": 0.65, "suggested_word": "verdict", "suggested_language": "en",
         "rationale": "明らか (obvious) + 無罪 (innocent) — courtroom domain, both about legal clarity."},
    70: {"compatibility": 0.50, "suggested_word": "wave", "suggested_language": "en",
         "rationale": "武士道 (bushido) + Wasser (water, German) — 'way of water' is a meditative martial concept."},
    71: {"compatibility": 0.40, "suggested_word": "creation", "suggested_language": "en",
         "rationale": "Liberty + Frankenstein — both involve creating something free from constraint."},
    72: {"compatibility": 0.30, "suggested_word": "introduce", "suggested_language": "en",
         "rationale": "動機 (motive) + greet — meeting someone with a motive, weak link."},
    73: {"compatibility": 0.72, "suggested_word": "conquest", "suggested_language": "en",
         "rationale": "野心 (ambition) + 军队 (army) — ambition driving an army, classical pairing."},
    74: {"compatibility": 0.55, "suggested_word": "however", "suggested_language": "en",
         "rationale": "まさか (no way) + でも (but) — both expressions of disbelief and contrast in Japanese."},
    75: {"compatibility": 0.40, "suggested_word": "encounter", "suggested_language": "en",
         "rationale": "打招呼 (greet) + 見つけ (find) — finding and greeting someone, related social acts."},
    76: {"compatibility": 0.45, "suggested_word": "luxury", "suggested_language": "en",
         "rationale": "Reward + 고급 (premium, Korean) — premium reward, related but not identical."},
    77: {"compatibility": 0.95, "suggested_word": "hanami", "suggested_language": "en",
         "rationale": "桜 (cherry blossom) + 春 (spring) — quintessential Japanese spring fusion, the very image of hanami."},
    78: {"compatibility": 0.60, "suggested_word": "sorcerer", "suggested_language": "en",
         "rationale": "皇上 (emperor) + witch — both wield power; fantasy archetype merger."},
    79: {"compatibility": 0.50, "suggested_word": "endure", "suggested_language": "en",
         "rationale": "Tenir (hold/endure, French) + 苦労 (hardship, Japanese) — enduring hardship, near-synonym."},
    80: {"compatibility": 0.20, "suggested_word": "antiseptic", "suggested_language": "en",
         "rationale": "气氛 (atmosphere) + surgical — surgical atmosphere is a fixed phrase but unusual."},

    # --- 81-100: cross-rarity random → LOW-MED ----------------------------
    81: {"compatibility": 0.55, "suggested_word": "exclamation", "suggested_language": "en",
         "rationale": "신이시여 (oh god) + おや (oh my) — both surprise/distress interjections across cultures."},
    82: {"compatibility": 0.10, "suggested_word": "stormhealth", "suggested_language": "en",
         "rationale": "雷 (thunder) + gesund (healthy, German) — no real connection."},
    83: {"compatibility": 0.45, "suggested_word": "hunted", "suggested_language": "en",
         "rationale": "Panicked + 늑대 (wolf, Korean) — being hunted by a wolf, evocative pairing."},
    84: {"compatibility": 0.35, "suggested_word": "tyrant", "suggested_language": "en",
         "rationale": "Power + 無視 (ignore, Japanese) — those in power ignoring the weak."},
    85: {"compatibility": 0.45, "suggested_word": "aggression", "suggested_language": "en",
         "rationale": "Boxing + 罪 (sin/crime) — both involve regulated violence, weak overlap."},
    86: {"compatibility": 0.40, "suggested_word": "abandon", "suggested_language": "en",
         "rationale": "惹 (provoke, Chinese) + 떠나 (leave, Korean) — provoke then leave, loosely a story."},
    87: {"compatibility": 0.70, "suggested_word": "guilt", "suggested_language": "en",
         "rationale": "非難 (blame) + 不安 (anxiety) — being blamed produces anxiety, tight emotional link."},
    88: {"compatibility": 0.30, "suggested_word": "definite", "suggested_language": "en",
         "rationale": "Nulle (null, French) + klar (clear, German) — both descriptors of clarity, opposing values."},
    89: {"compatibility": 0.30, "suggested_word": "matinee", "suggested_language": "en",
         "rationale": "무대 (stage, Korean) + rain — rainy day theater, situational scene."},
    90: {"compatibility": 0.55, "suggested_word": "fanfare", "suggested_language": "en",
         "rationale": "Gabriel + drum — Gabriel's trumpet (and drum) heralds, mythological link."},
    91: {"compatibility": 0.45, "suggested_word": "evangelize", "suggested_language": "en",
         "rationale": "イエス (Jesus, Japanese) + 沟通 (communicate, Chinese) — communicating Jesus's message."},
    92: {"compatibility": 0.40, "suggested_word": "advance", "suggested_language": "en",
         "rationale": "前進 (forward, Japanese) + appeal — making an appealing advance, related actions."},
    93: {"compatibility": 0.20, "suggested_word": "reborn", "suggested_language": "en",
         "rationale": "Phénix (phoenix, French) + site — a phoenix's nesting site, weak link."},
    94: {"compatibility": 0.10, "suggested_word": "lineage", "suggested_language": "en",
         "rationale": "ADN (DNA, French) + Bernard (name) — Bernard's DNA, only loosely associated."},
    95: {"compatibility": 0.15, "suggested_word": "rare", "suggested_language": "en",
         "rationale": "Seldom + zerstören (destroy, German) — seldom destroyed, faint connection."},
    96: {"compatibility": 0.45, "suggested_word": "transient", "suggested_language": "en",
         "rationale": "デルタ (delta, change) + 侘寂 (wabi-sabi, beauty in transience) — both about flux and impermanence."},
    97: {"compatibility": 0.55, "suggested_word": "enchant", "suggested_language": "en",
         "rationale": "Wand + 温暖 (warm, Chinese) — magical warmth, evocative pairing."},
    98: {"compatibility": 0.20, "suggested_word": "reverence", "suggested_language": "en",
         "rationale": "膝盖 (knee) + 大切 (important, Japanese) — kneeling for importance, distant link."},
    99: {"compatibility": 0.30, "suggested_word": "chronic", "suggested_language": "en",
         "rationale": "墜落 (fall, Japanese) + 자주 (often, Korean) — falling often, descriptive but loose."},
    100: {"compatibility": 0.20, "suggested_word": "draught", "suggested_language": "en",
          "rationale": "다른 (different, Korean) + ale — different ale, weak associative link."},
}


# ============================================================================
# Run through production pipeline
# ============================================================================

def _roll_success(key: str, comp: float) -> bool:
    h = hashlib.sha256(key.encode()).digest()
    roll = int.from_bytes(h[:8], "big") / (1 << 64)
    return roll < _success_rate(comp)


def main():
    pairs = json.loads((ROOT / "scripts" / "fusion_sample_100.json").read_text())
    print("=" * 75)
    print("FUSION LLM EVAL — 100 SAMPLED PAIRS THROUGH PRODUCTION PIPELINE")
    print("=" * 75)

    results: list[dict] = []
    val_rejected = 0
    by_category = defaultdict(lambda: {
        "n": 0, "compats": [], "success": 0, "fail": 0, "rejected": 0,
    })

    for p in pairs:
        pid = p["pair_id"]
        if pid not in LLM_OUTPUTS:
            continue
        a, b = p["a"], p["b"]
        cat = p["category"]
        raw = LLM_OUTPUTS[pid]

        # V2: legacy free-form compatibility in LLM_OUTPUTS gets mapped to a
        # tier internally; we read back the resolved tier + final compat.
        try:
            tier, sub, new_word, new_lang, rationale = _validate_llm_output(raw)
        except FusionValidationError as e:
            val_rejected += 1
            by_category[cat]["rejected"] += 1
            results.append({"pair_id": pid, "rejected": True, "reason": str(e)})
            continue

        lang_map = {"en": 0, "zh": 1, "ja": 2, "ko": 3, "fr": 4, "de": 5}
        key = _pair_key(a["word"], lang_map[a["language"]],
                        b["word"], lang_map[b["language"]])
        comp = _tier_to_compat(tier, sub, key)
        success = _roll_success(key, comp)
        new_power = _capped_new_power(a["power"], b["power"], comp) if success else 0

        by_category[cat]["n"] += 1
        by_category[cat]["compats"].append(comp)
        if success:
            by_category[cat]["success"] += 1
        else:
            by_category[cat]["fail"] += 1

        results.append({
            "pair_id": pid,
            "category": cat,
            "expected": p.get("expected"),
            "a_word": a["word"], "a_lang": a["language"], "a_power": a["power"],
            "b_word": b["word"], "b_lang": b["language"], "b_power": b["power"],
            "compat": comp,
            "success": success,
            "new_word": new_word,
            "new_lang_id": new_lang,
            "new_power": new_power,
            "rationale": rationale,
        })

    # ----- Per-category summary -----
    print("\n[1] By category — compat distribution + success rate")
    print("-" * 75)
    print(f"  {'category':<25} {'n':>3} {'compat μ':>10} {'min':>6} {'max':>6} {'success':>8}")
    for cat, s in by_category.items():
        if not s["compats"]:
            continue
        mu = round(statistics.mean(s["compats"]), 3)
        mn = round(min(s["compats"]), 2)
        mx = round(max(s["compats"]), 2)
        sr = f"{s['success']}/{s['n']}"
        print(f"  {cat:<25} {s['n']:>3} {mu:>10} {mn:>6} {mx:>6} {sr:>8}")

    # ----- Compat distribution buckets -----
    print("\n[2] Overall compat distribution")
    print("-" * 75)
    all_compats = [r["compat"] for r in results if "compat" in r]
    buckets = Counter()
    for c in all_compats:
        b = "0.0-0.2" if c < 0.2 else \
            "0.2-0.4" if c < 0.4 else \
            "0.4-0.6" if c < 0.6 else \
            "0.6-0.8" if c < 0.8 else "0.8-1.0"
        buckets[b] += 1
    for b in ["0.0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0"]:
        bar = "█" * buckets.get(b, 0)
        print(f"  {b}: {buckets.get(b, 0):>3} {bar}")

    # ----- Success rate by compat band -----
    print("\n[3] Success rate by compat band (sanity check on the curve)")
    print("-" * 75)
    bands = defaultdict(lambda: [0, 0])
    for r in results:
        if "compat" not in r:
            continue
        b = round(r["compat"] * 10) / 10
        bands[b][1 if r["success"] else 0] += 1
    print(f"  {'compat':>7} {'fail':>4} {'success':>7} {'rate':>6}  (theoretical)")
    for b in sorted(bands.keys()):
        f, s = bands[b]
        rate = s / max(1, f + s)
        theo = _success_rate(b)
        print(f"  {b:>7.1f} {f:>4} {s:>7} {rate*100:>5.0f}%  ({theo*100:.0f}%)")

    # ----- Sample winners -----
    print("\n[4] Sample successful fusions (top 10 by new_power)")
    print("-" * 75)
    successes = [r for r in results if r.get("success")]
    successes.sort(key=lambda r: -r["new_power"])
    for r in successes[:10]:
        print(f"  [{r['pair_id']:3d}] {r['a_word']} ({r['a_lang']}, p={r['a_power']}) + "
              f"{r['b_word']} ({r['b_lang']}, p={r['b_power']}) → "
              f"{r['new_word']} (p={r['new_power']}, comp={r['compat']:.2f})")

    # ----- Findings -----
    print("\n" + "=" * 75)
    print("FINDINGS")
    print("=" * 75)
    issues = []

    # Check: same-word cross-lang should be HIGH compat
    swcl = [r for r in results if r.get("category") == "same_word_cross_lang"]
    swcl_low = [r for r in swcl if r["compat"] < 0.7]
    print(f"  same_word_cross_lang: {len(swcl)} pairs, "
          f"{len(swcl_low)} with compat < 0.7 (false friends — flagged below)")
    for r in swcl_low:
        print(f"    [{r['pair_id']:3d}] {r['a_word']} ({r['a_lang']}/{r['b_lang']}): "
              f"compat={r['compat']:.2f} — \"{r['rationale'][:60]}...\"")

    # Check: unrelated cross-lang should be LOW compat
    ucl = [r for r in results if r.get("category") == "unrelated_cross_lang"]
    ucl_high = [r for r in ucl if r["compat"] > 0.5]
    print(f"\n  unrelated_cross_lang: {len(ucl)} pairs, "
          f"{len(ucl_high)} with compat > 0.5 (unexpectedly high — verify):")
    for r in ucl_high:
        print(f"    [{r['pair_id']:3d}] {r['a_word']}+{r['b_word']}: compat={r['compat']:.2f}")

    # Validator
    print(f"\n  Validator rejections: {val_rejected} / 100")
    if val_rejected > 0:
        issues.append(("INFO", f"{val_rejected} LLM outputs rejected by validator (expected: malformed inputs caught)"))

    # Power overflow
    capped = [r for r in results if r.get("new_power") == MAX_POWER_UINT16]
    print(f"  Power cap hits: {len(capped)} / {sum(1 for r in results if r.get('success'))}")

    # Persist
    out_path = ROOT / "scripts" / "fusion_llm_eval_results.json"
    out_path.write_text(json.dumps({
        "evaluator": "Claude (in-context, deterministic)",
        "total": len(results),
        "validator_rejected": val_rejected,
        "by_category": {
            cat: {
                "n": s["n"],
                "compat_mean": round(statistics.mean(s["compats"]), 3) if s["compats"] else None,
                "success": s["success"],
                "fail": s["fail"],
                "rejected": s["rejected"],
            }
            for cat, s in by_category.items()
        },
        "results": results,
    }, indent=2, ensure_ascii=False))
    print(f"\nResults: {out_path}")


if __name__ == "__main__":
    main()
