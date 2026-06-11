"""Phase A4 — adaptive-mode quality gate + retry ladder (Adaptive Low-Poly plan §7).

Pure, Blender-free: the gate's §7 threshold table and the cheap→expensive retry-rung
selection are exercised against synthetic metric records and reference baselines, the
same offline pattern the rest of the retopo control logic uses. The ``bpy`` baseline
measurement is done by the worker and fed in as plain numbers.
"""

from retopo_agent.geometry.adaptive_gate import (
    LADDER_ORDER,
    RUNG_DENSER_PROXY,
    RUNG_FEATURE_PROTECT,
    RUNG_RATIO_REAIM,
    RUNG_REPORT_FAILED,
    RUNG_SHRINKWRAP,
    RUNG_TRIQUAD_TWEAK,
    GateThresholds,
    ReferenceBaseline,
    evaluate_gate,
    next_rung,
)

# A representative reference baseline (humanstatue_low vs the 1M proxy, plan §7).
BASELINE = ReferenceBaseline(
    proxy_to_ref_max=0.80,
    proxy_to_ref_p99=0.40,
    ref_to_proxy_mean=0.05,
    ref_to_proxy_normal_dev=8.0,
    ref_vertex_count=3191,
)

# Metrics of a clean, in-band, silhouette-true candidate that should PASS.
GOOD = {
    "ngons": 0,
    "non_manifold_edges": 0,
    "faces": 5850,
    "vertex_count": 3100,
    "bbox_per_axis": {"x": 0.995, "y": 0.99, "z": 0.985},
    "proxy_to_low": {"max": 0.70, "p99": 0.35},
    "low_to_proxy": {"mean": 0.05, "normal_dev": 9.0},
}


def _gate(**overrides):
    metrics = {**GOOD, **overrides}
    return evaluate_gate(metrics, target_face_count=5850, baseline=BASELINE,
                         thresholds=GateThresholds())


def test_clean_candidate_passes_all_gates():
    gate = _gate()
    assert gate.passed is True
    assert gate.verdict == "pass"
    assert gate.hard_failures == []
    assert gate.soft_failures == []


def test_ngons_are_a_hard_failure():
    gate = _gate(ngons=2)
    assert gate.passed is False
    assert "ngons" in [c.name for c in gate.hard_failures]


def test_non_manifold_is_a_hard_failure():
    gate = _gate(non_manifold_edges=5)
    assert "non_manifold_edges" in [c.name for c in gate.hard_failures]


def test_bbox_coverage_below_098_truncation_is_hard():
    # A trident tine truncated along z shrinks that axis below the 0.98 floor.
    gate = _gate(bbox_per_axis={"x": 0.99, "y": 0.99, "z": 0.87})
    assert gate.passed_hard is False
    assert "bbox_coverage" in [c.name for c in gate.hard_failures]


def test_proxy_to_low_max_over_reference_is_hard():
    # A dropped feature -> a proxy region far from any low face, beyond ref x 1.25.
    gate = _gate(proxy_to_low={"max": 0.80 * 1.25 + 0.1, "p99": 0.35})
    assert "proxy_to_low_max" in [c.name for c in gate.hard_failures]


def test_face_count_out_of_band_is_hard():
    gate = _gate(faces=4000)  # ~32% under target
    assert "face_count_band" in [c.name for c in gate.hard_failures]


def test_vertex_count_is_only_a_sanity_warning():
    gate = _gate(vertex_count=3191 * 2)  # well over ref x 1.15
    assert gate.passed_hard is True
    assert gate.passed is True  # sanity warnings do not block
    assert "vertex_count" in [c.name for c in gate.sanity_warnings]


def test_soft_shape_failure_blocks_pass_and_triggers_retry():
    gate = _gate(low_to_proxy={"mean": 0.05 * 1.5 + 0.01, "normal_dev": 9.0})
    assert gate.passed_hard is True   # hard gates fine...
    assert gate.passed is False       # ...but a soft failure -> retry
    assert gate.verdict == "retry"
    assert "low_to_proxy_mean" in [c.name for c in gate.soft_failures]


# -- retry ladder rung selection (plan §7) ----------------------------------


def test_passing_gate_needs_no_rung():
    assert next_rung(_gate(), attempted_rungs=[]) == ""


def test_band_miss_picks_ratio_reaim_first():
    gate = _gate(faces=4000)
    assert next_rung(gate, attempted_rungs=[]) == RUNG_RATIO_REAIM


def test_ngon_failure_picks_triquad_tweak():
    gate = _gate(ngons=4)
    assert next_rung(gate, attempted_rungs=[]) == RUNG_TRIQUAD_TWEAK


def test_extremity_thinning_picks_feature_protection():
    gate = _gate(bbox_per_axis={"x": 0.99, "y": 0.99, "z": 0.90})
    assert next_rung(gate, attempted_rungs=[]) == RUNG_FEATURE_PROTECT


def test_soft_shape_failure_picks_shrinkwrap():
    gate = _gate(low_to_proxy={"mean": 1.0, "normal_dev": 9.0})
    assert next_rung(gate, attempted_rungs=[]) == RUNG_SHRINKWRAP


def test_hard_failures_take_priority_over_soft_in_ladder_order():
    # Both a coverage (hard, feature-protect) and a shape (soft, shrinkwrap) fail;
    # the cheaper-in-ladder rung that addresses an actual failure wins.
    gate = _gate(
        bbox_per_axis={"x": 0.99, "y": 0.99, "z": 0.90},
        low_to_proxy={"mean": 1.0, "normal_dev": 9.0},
    )
    rung = next_rung(gate, attempted_rungs=[])
    assert rung == RUNG_FEATURE_PROTECT  # earlier in LADDER_ORDER than shrinkwrap


def test_spent_rung_escalates_along_the_ladder():
    gate = _gate(bbox_per_axis={"x": 0.99, "y": 0.99, "z": 0.90})
    # feature-protection (the targeted rung) already tried and still failing ->
    # escalate to the next unspent ladder rung (cheap→expensive, plan §7)...
    assert next_rung(gate, attempted_rungs=[RUNG_FEATURE_PROTECT]) == RUNG_SHRINKWRAP
    # ...and once that too is spent, on to the denser-proxy fallback.
    assert next_rung(
        gate, attempted_rungs=[RUNG_FEATURE_PROTECT, RUNG_SHRINKWRAP]
    ) == RUNG_DENSER_PROXY


def test_exhausted_ladder_reports_failed():
    gate = _gate(bbox_per_axis={"x": 0.99, "y": 0.99, "z": 0.90})
    attempted = [r for r in LADDER_ORDER if r != RUNG_REPORT_FAILED]
    assert next_rung(gate, attempted_rungs=attempted) == RUNG_REPORT_FAILED
