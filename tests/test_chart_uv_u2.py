"""U2–U4 — pure pipeline helpers (chart-UV plan §6–§8). Blender-free parts only."""

import numpy as np

from chart_uv_agent.fixtures import build_displaced_sphere
from chart_uv_agent.gate import ChartGateConfig, evaluate_chart_gate
from chart_uv_agent.pipeline import (
    _better, _chart_metrics, _split_flipped_charts, _worst_stretch_chart,
)
from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges
from chart_uv_agent.unwrap import flipped_faces
from uv_agent.geometry.evaluation import evaluate_uv_solution
from uv_agent.geometry.solution import UVMap
from uv_agent.io.fixtures import build_grid_plane
from uv_agent.planner.island_planner import plan_islands


def test_flipped_faces_detects_reversed_winding():
    plane = build_grid_plane(nx=3, ny=3)
    uvmap = UVMap.for_mesh(plane)
    # Lay every face out with its own XY (positive area) except face 0, which we flip.
    for f in plane.faces:
        for li in f.loop_indices:
            x, y, _ = plane.vertices[plane.loops[li].vertex_id].co
            uvmap.set(li, x, y)
    f0 = plane.faces[0]
    for li in f0.loop_indices:  # mirror face 0's UVs in u -> negative signed area
        u, v = uvmap.get(li)
        uvmap.set(li, -u, v)
    flips = flipped_faces(plane, uvmap)
    assert 0 in flips


def test_no_flips_for_consistent_layout():
    plane = build_grid_plane(nx=3, ny=3)
    uvmap = UVMap.for_mesh(plane)
    for f in plane.faces:
        for li in f.loop_indices:
            x, y, _ = plane.vertices[plane.loops[li].vertex_id].co
            uvmap.set(li, x, y)
    assert flipped_faces(plane, uvmap) == []


def test_chart_metrics_has_all_gate_keys():
    plane = build_grid_plane(nx=3, ny=3)
    uvmap = UVMap.for_mesh(plane)
    plan = plan_islands(plane, angle_threshold=1e9, split_by_material=False)
    ev = evaluate_uv_solution(plane, plan, uvmap)
    m = _chart_metrics(plane, uvmap, ev)
    for key in ("overlap_ratio", "stretch_score", "packing_efficiency", "island_count",
                "small_island_ratio", "texel_density_variance", "vt_v_ratio",
                "uv_bounds_ok", "fallback_used"):
        assert key in m
    assert m["fallback_used"] is False


def test_worst_stretch_chart_picks_highest_area_weighted():
    mesh = build_displaced_sphere(segments=12, rings=8)
    charts = flood_charts(mesh, mandatory_seam_edges(mesh))
    fstr = np.zeros(mesh.face_count)
    target = charts[0]
    for f in target:
        fstr[f] = 5.0  # make the first chart the worst
    assert _worst_stretch_chart(mesh, charts, fstr) is target


def test_split_flipped_charts_targets_only_flipped():
    mesh = build_displaced_sphere(segments=12, rings=8)
    seams = set(mandatory_seam_edges(mesh))
    charts = flood_charts(mesh, seams)
    face_chart = {f: cid for cid, fs in enumerate(charts) for f in fs}
    flipped = [charts[0][0]]  # one face in chart 0
    new = _split_flipped_charts(mesh, seams, face_chart, charts, flipped)
    assert all(eid not in seams for eid in new)


# -- _better selection + gate verdicts --------------------------------------

def _gate(**m):
    base = {"mandatory_90_missing": 0, "mandatory_90_uv_unsplit": 0, "worst_island_distortion": 0.4,
            "overlap_ratio": 0.0, "raster_overlap_ratio": 0.001, "stretch_score": 0.3,
            "packing_efficiency": 0.75, "island_count": 30, "small_island_ratio": 0.2,
            "texel_density_variance": 0.5, "vt_v_ratio": 1.4, "uv_bounds_ok": True,
            "fallback_used": False, "convexity_mean": 0.78, "convexity_p10": 0.6,
            "boundary_smoothness_mean": 1.4, "tendril_count": 0}
    metrics = {**base, **m}
    return metrics, evaluate_chart_gate(metrics, config=ChartGateConfig())


def test_better_prefers_passing_gate():
    pm, pg = _gate()                       # passes
    fm, fg = _gate(stretch_score=1.5)       # fails (hard distortion gate)
    assert _better(pm, pg, {"gate": fg, "metrics": fm})
    assert not _better(fm, fg, {"gate": pg, "metrics": pm})


def test_better_among_failing_prefers_lower_stretch():
    # Both fail a hard NON-stretch gate (texel density), so _better falls through to the
    # lower-stretch tiebreak.
    am, ag = _gate(texel_density_variance=2.0, stretch_score=0.2)
    bm, bg = _gate(texel_density_variance=2.0, stretch_score=0.4)
    assert _better(am, ag, {"gate": bg, "metrics": bm})
