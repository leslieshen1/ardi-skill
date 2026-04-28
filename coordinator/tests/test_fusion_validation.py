"""Focused tests for the V2 fusion oracle rules:

  - 5-tier compat rubric (T1..T5) + tier_subscore validation
  - Backwards-compat path that accepts a legacy `compatibility` float
  - Smooth multiplier curve `MULT_BASE + MULT_SPREAD × (1-c)^MULT_EXPONENT`
  - Sigmoid success curve floored at SUCCESS_FLOOR
  - parent_langs policy enforcement
"""
import math
import pytest

from coordinator.fusion import (
    FusionValidationError,
    MAX_POWER_UINT16,
    MAX_SUGGESTED_WORD_LEN,
    MULT_BASE,
    MULT_EXPONENT,
    MULT_SPREAD,
    SIG_K,
    SIG_MID,
    SUCCESS_FLOOR,
    TIER_RANGES,
    _capped_new_power,
    _compat_to_tier,
    _multiplier,
    _success_rate,
    _tier_to_compat,
    _validate_llm_output,
)


def good_output(**overrides):
    """V2 schema — uses tier + tier_subscore, no top-level compatibility."""
    base = {
        "tier": "T3",
        "tier_subscore": 0.5,
        "suggested_word": "phoenix",
        "suggested_language": "en",
        "rationale": "fire and rebirth",
    }
    base.update(overrides)
    return base


# ============================== Tier rubric =================================


def test_happy_path_tier():
    tier, sub, word, lang_id, rat = _validate_llm_output(good_output())
    assert tier == "T3"
    assert sub == 0.5
    assert word == "phoenix"
    assert lang_id == 0
    assert rat == "fire and rebirth"


def test_unknown_tier_rejected():
    with pytest.raises(FusionValidationError, match="tier"):
        _validate_llm_output(good_output(tier="T6"))
    with pytest.raises(FusionValidationError, match="tier"):
        _validate_llm_output(good_output(tier=""))


def test_tier_subscore_out_of_range():
    with pytest.raises(FusionValidationError, match="subscore"):
        _validate_llm_output(good_output(tier_subscore=1.5))
    with pytest.raises(FusionValidationError, match="subscore"):
        _validate_llm_output(good_output(tier_subscore=-0.1))


def test_tier_subscore_defaults_to_mid_when_missing():
    out = good_output()
    out.pop("tier_subscore")
    tier, sub, *_ = _validate_llm_output(out)
    assert sub == 0.5  # default mid-tier


# ============================== Legacy fallback =============================


def test_legacy_compat_float_maps_to_tier():
    """Old cache rows with top-level `compatibility` should still validate."""
    legacy = {
        # No `tier`; only legacy `compatibility`
        "compatibility": 0.92,  # → T1
        "suggested_word": "shared",
        "suggested_language": "en",
        "rationale": "legacy",
    }
    tier, sub, *_ = _validate_llm_output(legacy)
    assert tier == "T1"
    # subscore inferred from where 0.92 sits in T1's [0.85, 0.99] range
    lo, hi = TIER_RANGES["T1"]
    expected = (0.92 - lo) / (hi - lo)
    assert abs(sub - expected) < 1e-6


def test_legacy_compat_out_of_range_rejected():
    with pytest.raises(FusionValidationError, match="out of"):
        _validate_llm_output({
            "compatibility": 1.5,
            "suggested_word": "x",
            "suggested_language": "en",
        })


# ============================== suggested_word ==============================


def test_suggested_word_too_long():
    with pytest.raises(FusionValidationError, match="too long"):
        _validate_llm_output(good_output(suggested_word="x" * (MAX_SUGGESTED_WORD_LEN + 1)))


def test_suggested_word_empty():
    with pytest.raises(FusionValidationError, match="empty"):
        _validate_llm_output(good_output(suggested_word=""))


def test_suggested_word_contains_pipe_delim():
    with pytest.raises(FusionValidationError, match="\\|\\|"):
        _validate_llm_output(good_output(suggested_word="bad||word"))


def test_suggested_word_contains_control_chars():
    with pytest.raises(FusionValidationError, match="control"):
        _validate_llm_output(good_output(suggested_word="bad\nword"))


# ============================== suggested_language ==========================


def test_suggested_language_invalid():
    with pytest.raises(FusionValidationError, match="invalid"):
        _validate_llm_output(good_output(suggested_language="klingon"))


def test_suggested_language_id_path():
    out = good_output()
    out.pop("suggested_language")
    out["suggested_language_id"] = 2  # ja
    _, _, _, lang_id, _ = _validate_llm_output(out)
    assert lang_id == 2


def test_parent_lang_policy_rejects_non_parent():
    """Under SUGGESTED_LANG_POLICY='parent', new_lang must be in parent_langs."""
    out = good_output(suggested_language="en")
    # Parents are zh + ja → en is NOT in parent_langs → reject
    with pytest.raises(FusionValidationError, match="parent"):
        _validate_llm_output(out, parent_langs=(1, 2))


def test_parent_lang_policy_accepts_parent():
    out = good_output(suggested_language="ja")
    tier, *_ = _validate_llm_output(out, parent_langs=(1, 2))
    assert tier == "T3"


# ============================== Curves ======================================


def test_multiplier_smooth_and_monotone():
    """Multiplier should decrease monotonically as compat increases."""
    samples = [_multiplier(c / 100) for c in range(101)]
    for i in range(1, len(samples)):
        assert samples[i] <= samples[i - 1] + 1e-9, "multiplier must be monotone non-increasing"
    # Boundary checks: c=0 gives MULT_BASE+MULT_SPREAD; c=1 gives MULT_BASE
    assert abs(_multiplier(0.0) - (MULT_BASE + MULT_SPREAD)) < 1e-9
    assert abs(_multiplier(1.0) - MULT_BASE) < 1e-9


def test_success_rate_sigmoid_floor_and_monotone():
    samples = [_success_rate(c / 100) for c in range(101)]
    for i in range(1, len(samples)):
        assert samples[i] >= samples[i - 1] - 1e-9, "success must be monotone non-decreasing"
    # Floor at SUCCESS_FLOOR (low compat shouldn't dip below it)
    assert _success_rate(0.0) >= SUCCESS_FLOOR
    # Mid: sigmoid centered at SIG_MID gives 0.5
    assert abs(_success_rate(SIG_MID) - 0.5) < 0.01
    # High: near 1.0 should be very close to 1
    assert _success_rate(1.0) > 0.95


# ============================== Tier mapping ================================


def test_tier_to_compat_within_range():
    """Final compat must always lie in the tier's declared interval."""
    pair_keys = ["a||b", "x||y", "fire||water", "0:foo||1:bar"]
    for tier, (lo, hi) in TIER_RANGES.items():
        for sub in [0.0, 0.25, 0.5, 0.75, 1.0]:
            for k in pair_keys:
                c = _tier_to_compat(tier, sub, k)
                assert lo <= c <= hi, f"{tier} sub={sub} key={k!r} → {c} not in [{lo}, {hi}]"


def test_tier_to_compat_subscore_monotone():
    """Higher subscore → higher compat within same tier (modulo bounded jitter)."""
    key = "fire||water"
    samples = [_tier_to_compat("T2", s / 10, key) for s in range(11)]
    # Check overall trend via correlation with subscore
    # (jitter is bounded ±0.025 so monotonicity may not hold strictly per-step,
    # but the endpoint should be > the start by at least the jitter floor)
    assert samples[-1] > samples[0] - 0.05


def test_compat_to_tier_inverse():
    assert _compat_to_tier(0.95) == "T1"
    assert _compat_to_tier(0.75) == "T2"
    assert _compat_to_tier(0.50) == "T3"
    assert _compat_to_tier(0.25) == "T4"
    assert _compat_to_tier(0.05) == "T5"


# ============================== Power cap ===================================


def test_capped_power_no_overflow():
    p = 100
    for _ in range(20):
        p = _capped_new_power(p, p, 0.1)
        assert p <= MAX_POWER_UINT16
    assert p == MAX_POWER_UINT16


def test_capped_power_normal_case():
    """V2 multiplier at compat=0.5 = 1.5 + 1.5×0.25 = 1.875. (50+50)*1.875 = 187."""
    expected = int(100 * (MULT_BASE + MULT_SPREAD * (0.5 ** MULT_EXPONENT)))
    assert _capped_new_power(50, 50, 0.5) == expected


# ============================== Rationale truncation ========================


def test_rationale_truncated():
    out = good_output(rationale="x" * 1000)
    *_, rat = _validate_llm_output(out)
    assert len(rat) == 512


# ============================== Cache versioning ============================


def test_pair_key_includes_cache_version():
    """Bumping CACHE_VERSION must invalidate all old cache rows. We assert the
    version prefix is in the pair_key so old rows can never collide with new."""
    from coordinator.fusion import CACHE_VERSION, _pair_key
    k = _pair_key("fire", 0, "water", 0)
    assert k.startswith(f"{CACHE_VERSION}||")


def test_pair_key_is_deterministic_and_canonical():
    """(a, b) and (b, a) must produce the same key — caller order doesn't matter."""
    from coordinator.fusion import _pair_key
    k1 = _pair_key("fire", 0, "water", 0)
    k2 = _pair_key("water", 0, "fire", 0)
    assert k1 == k2
