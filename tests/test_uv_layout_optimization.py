"""Blender-free unit tests for the UV Layout Optimization Loop
(UV_LAYOUT_OPTIMIZATION_LOOP_PLAN §12.1). Cover scoring, the hard-reject rule (incl. the
plan §3.1 decision that mandatory-90 audits are NOT a reject condition in user/reference
mode), baseline-aware best-candidate selection, the candidate axis-product, and the
preset builder."""

from __future__ import annotations

import math

from chart_uv_agent.layout_optimization import (
    BASELINE_SPEC, LayoutCandidate, LayoutOptimizationConfig, candidate_is_valid,
    candidate_specs, default_score_weights, layout_score, make_config,
    run_layout_optimization, select_best_candidate, spec_id,
)


def _clean_metrics(**over):
    m = {
        "raster_overlap_ratio": 0.0, "overlap_ratio": 0.0, "stretch_score": 0.07,
        "worst_island_distortion": 0.20, "texel_density_variance": 0.001,
        "packing_efficiency": 0.58, "small_island_ratio": 0.1,
        "uv_bounds_ok": True, "fallback_used": False,
    }
    m.update(over)
    return m


# --- 1. score calculation -------------------------------------------------------------

def test_layout_score_matches_weighted_sum():
    w = default_score_weights()
    m = _clean_metrics()
    expected = (4.0 * 0.07 + 3.0 * 0.20 + 2.0 * 0.001 + 2.0 * 0.0
                + 1.0 * 0.0 - 1.5 * 0.58 + 0.2 * 0.1)
    assert layout_score(m, w) == expected


def test_lower_stretch_scores_better():
    good = _clean_metrics(stretch_score=0.05)
    bad = _clean_metrics(stretch_score=0.30)
    assert layout_score(good) < layout_score(bad)


def test_higher_packing_scores_better():
    loose = _clean_metrics(packing_efficiency=0.50)
    tight = _clean_metrics(packing_efficiency=0.70)
    assert layout_score(tight) < layout_score(loose)


# --- 2 & 3. hard reject + mandatory-90 ignored in user/reference mode -----------------

def test_clean_candidate_is_valid():
    ok, reason = candidate_is_valid(_clean_metrics(), mode="user_reference")
    assert ok and reason == "valid"


def test_user_reference_score_ignores_mandatory_90_failures():
    metrics = _clean_metrics(mandatory_90_missing=85, mandatory_90_uv_unsplit=85)
    ok, _ = candidate_is_valid(metrics, mode="user_reference")
    assert ok


def test_raster_overlap_rejects():
    ok, reason = candidate_is_valid(_clean_metrics(raster_overlap_ratio=0.5),
                                    mode="user_reference")
    assert not ok and reason == "raster_overlap"


def test_signed_area_overlap_rejects():
    ok, reason = candidate_is_valid(_clean_metrics(overlap_ratio=0.5), mode="user_reference")
    assert not ok and reason == "overlap"


def test_out_of_bounds_rejects():
    ok, reason = candidate_is_valid(_clean_metrics(uv_bounds_ok=False), mode="user_reference")
    assert not ok and reason == "uv_bounds_ok_false"


def test_fallback_rejects():
    ok, reason = candidate_is_valid(_clean_metrics(fallback_used=True), mode="user_reference")
    assert not ok and reason == "fallback_used"


def test_non_finite_metric_rejects():
    ok, reason = candidate_is_valid(_clean_metrics(stretch_score=math.nan),
                                    mode="user_reference")
    assert not ok and reason == "non_finite_stretch_score"


# --- 4 & 5. best selection + baseline retention ---------------------------------------

def _cand(cid, metrics, cfg, accepted=True):
    return LayoutCandidate(id=cid, unwrap_method="MINIMUM_STRETCH", minimize_iters=0,
                           margin=0.005, pack_shape="CONCAVE", rotate=True, metrics=metrics,
                           score=layout_score(metrics, cfg.score_weights), accepted=accepted)


def test_select_best_picks_lowest_eligible_score():
    cfg = LayoutOptimizationConfig()
    base = _clean_metrics()
    base_score = layout_score(base, cfg.score_weights)
    # A clearly better candidate: less stretch + tighter pack, no regression.
    better = _clean_metrics(stretch_score=0.04, packing_efficiency=0.66)
    cands = [_cand("baseline", base, cfg), _cand("winner", better, cfg)]
    sel, info = select_best_candidate(cands, base, base_score, cfg)
    assert sel == "winner" and info["reason"] == "best_score"


def test_marginal_improvement_keeps_baseline():
    cfg = LayoutOptimizationConfig()
    base = _clean_metrics()
    base_score = layout_score(base, cfg.score_weights)
    # Improvement well below the 1% threshold (baseline score is small, so make the
    # stretch delta tiny — a 0.00005 drop ⇒ 0.0002 score gain, under the threshold).
    tiny = _clean_metrics(stretch_score=0.06995)
    cands = [_cand("tiny", tiny, cfg)]
    sel, info = select_best_candidate(cands, base, base_score, cfg)
    assert sel is None and info["reason"] == "below_min_improvement"


def test_packing_win_with_stretch_regression_is_not_eligible():
    cfg = LayoutOptimizationConfig()
    base = _clean_metrics()
    base_score = layout_score(base, cfg.score_weights)
    # Big packing win but stretch blows past the 1.05 regression factor → ineligible.
    regressed = _clean_metrics(packing_efficiency=0.90, stretch_score=0.20)
    cands = [_cand("regressed", regressed, cfg)]
    sel, info = select_best_candidate(cands, base, base_score, cfg)
    assert sel is None and info["reason"] == "no_eligible_candidate"


def test_invalid_candidate_never_selected():
    cfg = LayoutOptimizationConfig()
    base = _clean_metrics()
    base_score = layout_score(base, cfg.score_weights)
    overlapping = _clean_metrics(stretch_score=0.01, raster_overlap_ratio=0.9)
    cands = [_cand("bad", overlapping, cfg, accepted=False)]
    sel, info = select_best_candidate(cands, base, base_score, cfg)
    assert sel is None and info["reason"] == "no_eligible_candidate"


# --- 6. config preset -----------------------------------------------------------------

def test_make_config_user_reference_default():
    cfg = make_config("user_reference")
    assert cfg.enabled and cfg.mode == "user_reference"
    assert cfg.max_candidates == 24


def test_make_config_respects_max_candidates_override():
    cfg = make_config("user_reference", max_candidates=6)
    assert cfg.max_candidates == 6


# --- 7. candidate axis-product --------------------------------------------------------

def test_first_candidate_is_the_baseline_spec():
    specs = candidate_specs(LayoutOptimizationConfig(max_candidates=24))
    first = specs[0]
    for k in ("unwrap_method", "minimize_iters", "margin", "pack_shape", "rotate"):
        assert first[k] == BASELINE_SPEC[k]


def test_slim_candidates_never_minimize():
    specs = candidate_specs(LayoutOptimizationConfig(max_candidates=64))
    for s in specs:
        if s["unwrap_method"] == "MINIMUM_STRETCH":
            assert s["minimize_iters"] == 0


def test_angle_based_carries_minimize_iters():
    specs = candidate_specs(LayoutOptimizationConfig(max_candidates=64))
    abf_iters = {s["minimize_iters"] for s in specs if s["unwrap_method"] == "ANGLE_BASED"}
    assert abf_iters & {10, 30}


def test_candidate_cap_respected():
    specs = candidate_specs(LayoutOptimizationConfig(max_candidates=5))
    assert len(specs) == 5


def test_spec_id_is_readable_and_stable():
    assert spec_id(BASELINE_SPEC) == "slim_concave_m005"
    abf = {"unwrap_method": "ANGLE_BASED", "minimize_iters": 30, "margin": 0.002,
           "pack_shape": "AABB", "rotate": True}
    assert spec_id(abf) == "abf_aabb_m002_min30"


# --- driver integration (pure callback, no Blender) -----------------------------------

def test_run_layout_optimization_selects_and_reports():
    cfg = LayoutOptimizationConfig(max_candidates=4)
    baseline = _clean_metrics()

    def measure(spec):
        # The AABB candidate packs better with no regression; everything else == baseline.
        if spec["pack_shape"] == "AABB":
            return _clean_metrics(packing_efficiency=0.70, stretch_score=0.05)
        return _clean_metrics()

    res = run_layout_optimization(measure, baseline, cfg, mode="user_reference")
    assert not res.kept_baseline
    assert res.score_after < res.score_before
    rep = res.report()
    assert rep["enabled"] and rep["candidates"]
    summ = res.summary()
    assert summ["candidate_count"] == len(res.candidates)


def test_run_layout_optimization_keeps_baseline_when_no_winner():
    cfg = LayoutOptimizationConfig(max_candidates=4)
    baseline = _clean_metrics()

    def measure(spec):
        return _clean_metrics()  # nothing beats baseline

    res = run_layout_optimization(measure, baseline, cfg, mode="user_reference")
    assert res.kept_baseline
    assert res.selected_spec["unwrap_method"] == BASELINE_SPEC["unwrap_method"]
