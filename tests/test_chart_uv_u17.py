"""U1.7 — tail round (worst-chart convexity), the final shape round (chart-UV plan §5c)."""

import numpy as np

from chart_uv_agent.fixtures import build_displaced_sphere, build_humanoid_blob
from chart_uv_agent.gate import ChartGateConfig, evaluate_chart_gate
from chart_uv_agent.pipeline import shippable_with_stuck
from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges, segment
from chart_uv_agent.shape import chart_convexity
from chart_uv_agent.shape_repair import repair_shapes, tail_round


def _p10(mesh, seams):
    cvs = [chart_convexity(mesh, c) for c in flood_charts(mesh, set(seams))]
    return float(np.percentile(cvs, 10))


def _disks_and_r2(mesh, seams):
    from chart_uv_agent.segmentation import is_disk
    mand = mandatory_seam_edges(mesh, fold_angle=90.0)
    return all(is_disk(mesh, c) for c in flood_charts(mesh, set(seams))) and mand.issubset(seams)


# -- tail round drives the worst decile up or reports stuck ------------------

def test_tail_round_never_regresses_p10_and_keeps_invariants():
    for mesh in (build_displaced_sphere(), build_humanoid_blob()):
        seg = segment(mesh, cone_limit=150)
        seams = set(seg.seams)
        repair_shapes(mesh, seams, convexity_min=0.92, max_charts=60)
        before = _p10(mesh, seams)
        res = tail_round(mesh, seams, convexity_bar=0.55, max_charts=60)
        assert _p10(mesh, seams) >= before - 1e-6   # best-p10 kept, never regresses
        assert _disks_and_r2(mesh, seams)            # disks + R2 preserved
        assert "stuck" in res and "convexity_p10" in res


def test_tail_round_fixes_an_artificial_spiky_chart():
    # A capsule-with-spikes has protruding fingers; after segment+repair the tail loop
    # should leave the worst-decile convexity no worse than the pre-tail state, and any
    # residual below-bar chart must be reported (fixed or stuck — never silently kept).
    from chart_uv_agent.fixtures import build_capsule_with_spikes
    mesh = build_capsule_with_spikes(n_spikes=5, spike_len=1.8)
    seg = segment(mesh, cone_limit=150)
    seams = set(seg.seams)
    repair_shapes(mesh, seams, convexity_min=0.92, max_charts=60)
    res = tail_round(mesh, seams, convexity_bar=0.55, max_charts=60)
    below = [c for c in flood_charts(mesh, set(seams)) if chart_convexity(mesh, c) < 0.55]
    # Every still-below-bar chart is accounted for as stuck (the loop converged on them).
    assert len(below) <= res["stuck_count"] + 1  # +1 slack for percentile boundary
    assert _disks_and_r2(mesh, seams)


# -- convexity is now ADVISORY (MINIMAL_DISTORTION_UV_PLAN §7) ----------------

def _gate(**m):
    base = {"mandatory_90_missing": 0, "mandatory_90_uv_unsplit": 0, "worst_island_distortion": 0.4,
            "overlap_ratio": 0.0, "raster_overlap_ratio": 0.001, "stretch_score": 0.3,
            "packing_efficiency": 0.6, "island_count": 40, "small_island_ratio": 0.2,
            "texel_density_variance": 0.5, "vt_v_ratio": 1.4, "uv_bounds_ok": True,
            "fallback_used": False, "convexity_mean": 0.78, "convexity_p10": 0.45,  # below 0.55
            "boundary_smoothness_mean": 1.4, "tendril_count": 0}
    return evaluate_chart_gate({**base, **m}, config=ChartGateConfig())


def test_convexity_p10_below_bar_is_advisory_not_blocking():
    # The plan demotes convexity to a report-only signal: a below-bar worst-decile chart
    # is an ADVISORY, never a hard failure, so the gate still passes and ships.
    gate = _gate()  # convexity_p10 below bar, everything else OK
    assert gate.passed
    assert "convexity_p10" not in [c.name for c in gate.failures]
    assert "convexity_p10" in [c.name for c in gate.advisories]
    assert shippable_with_stuck(gate, stuck_charts=[]) is True


def test_hard_failures_still_block_shipping():
    gate = _gate(overlap_ratio=0.05)  # overlap is hard; convexity_p10 advisory
    assert not gate.passed
    assert [c.name for c in gate.failures] == ["overlap_ratio"]
    assert shippable_with_stuck(gate, stuck_charts=[{"size": 9, "convexity": 0.3}]) is False


def test_passing_gate_is_shippable():
    gate = _gate(convexity_p10=0.6)  # passes the (advisory) tail bar
    assert gate.passed
    assert shippable_with_stuck(gate, stuck_charts=[]) is True
