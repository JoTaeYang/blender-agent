"""Tests for the MVP 3 candidate-summary normalization (plan §5, Session C).

The candidate summary is normalized from a real
``chart_uv_agent.layout_optimization.LayoutOptimizationResult`` — that module is
pure (Blender-free), so we drive it with a fake ``measure_candidate`` callback and
assert the normalized ``candidate_summary.json`` matches the plan §5 schema:
UI-friendly rows, a rejected list, a consistent selected id, and the baseline id.
"""

import importlib.util
import os

from chart_uv_agent.layout_optimization import (
    BASELINE_SPEC, make_config, run_layout_optimization, spec_id,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_contract():
    path = os.path.join(_ROOT, "worker", "app_uv_generate_contract.py")
    spec = importlib.util.spec_from_file_location("app_uv_generate_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract = _load_contract()


def _metrics(*, packing=0.58, raster=0.0, bounds=True):
    return {
        "stretch_score": 0.06866, "worst_island_distortion": 0.203,
        "raster_overlap_ratio": raster, "overlap_ratio": 0.0,
        "texel_density_variance": 2e-6, "packing_efficiency": packing,
        "small_island_ratio": 0.1, "uv_bounds_ok": bounds, "island_count": 52,
    }


def _run(max_candidates=24, *, winner_margin=0.002, overlap_on_aabb=False):
    """Drive a real layout-optimization sweep with a deterministic fake measure.

    The ``winner_margin`` candidate packs best (lowest score wins); AABB packs can
    optionally be given raster overlap so they are hard-rejected (plan §5/§7.4)."""
    cfg = make_config("user_reference", max_candidates=max_candidates, enabled=True)

    def measure(s):
        packing = 0.59 if s["margin"] == winner_margin else 0.58
        raster = 0.02 if (overlap_on_aabb and s["pack_shape"] == "AABB") else 0.0
        return _metrics(packing=packing, raster=raster, bounds=True)

    res = run_layout_optimization(measure, measure(BASELINE_SPEC), cfg)
    return res, cfg


def test_candidate_summary_schema_and_baseline_id():
    res, cfg = _run()
    cs = contract.normalize_candidate_summary(
        res.report(), baseline_candidate_id=spec_id(BASELINE_SPEC),
        score_weights=cfg.score_weights, max_candidates=cfg.max_candidates,
        average_scale=cfg.average_scale)
    assert cs["schema_version"] == contract.SCHEMA_VERSION
    assert cs["baseline_candidate_id"] == spec_id(BASELINE_SPEC) == "slim_concave_m005"
    assert cs["selected_candidate_id"] == res.selected_candidate_id
    assert cs["kept_baseline"] is res.kept_baseline
    assert cs["score_weights"] == cfg.score_weights
    # every candidate row has the plan §5 shape
    row = cs["candidates"][0]
    for key in ("id", "unwrap_method", "minimize_iters", "margin", "pack_shape",
                "rotate", "average_scale", "accepted", "reason", "score", "metrics"):
        assert key in row, key
    assert set(row["metrics"].keys()) <= set(contract.CANDIDATE_METRIC_KEYS)


def test_selected_id_consistent_across_summary_and_block():
    res, cfg = _run()
    rep = res.report()
    cs = contract.normalize_candidate_summary(rep, baseline_candidate_id=spec_id(BASELINE_SPEC),
                                              score_weights=cfg.score_weights)
    block = contract.build_layout_optimization_block(rep)
    assert cs["selected_candidate_id"] == block["selected_candidate_id"] == res.selected_candidate_id


def test_candidate_count_capped_by_option():
    res, cfg = _run(max_candidates=5)
    cs = contract.normalize_candidate_summary(res.report(),
                                              baseline_candidate_id=spec_id(BASELINE_SPEC),
                                              score_weights=cfg.score_weights, max_candidates=5)
    assert len(cs["candidates"]) <= 5


def test_rejected_list_records_overlap_candidates():
    res, cfg = _run(overlap_on_aabb=True)
    cs = contract.normalize_candidate_summary(res.report(),
                                              baseline_candidate_id=spec_id(BASELINE_SPEC),
                                              score_weights=cfg.score_weights)
    # AABB candidates carry raster overlap -> rejected with a reason (plan §5).
    assert cs["rejected"], "expected at least one rejected candidate"
    assert all(r["reason"] for r in cs["rejected"])
    rejected_ids = {r["id"] for r in cs["rejected"]}
    not_accepted = {c["id"] for c in cs["candidates"] if not c["accepted"]}
    assert rejected_ids == not_accepted
    # the selected candidate is never in the rejected set.
    assert cs["selected_candidate_id"] not in rejected_ids


def test_empty_report_yields_valid_empty_summary():
    cs = contract.normalize_candidate_summary(None, baseline_candidate_id="slim_concave_m005",
                                              score_weights=contract.DEFAULT_SCORE_WEIGHTS)
    assert cs["schema_version"] == contract.SCHEMA_VERSION
    assert cs["candidates"] == []
    assert cs["rejected"] == []
    assert cs["selected_candidate_id"] is None


def test_kept_baseline_case():
    # All candidates measure identically -> no candidate beats the baseline by the
    # 1% improvement threshold, so the baseline is retained (plan §14, §5).
    cfg = make_config("user_reference", max_candidates=24, enabled=True)
    flat = _metrics(packing=0.58)
    res = run_layout_optimization(lambda s: dict(flat), dict(flat), cfg)
    cs = contract.normalize_candidate_summary(res.report(),
                                              baseline_candidate_id=spec_id(BASELINE_SPEC),
                                              score_weights=cfg.score_weights)
    assert cs["kept_baseline"] is True
