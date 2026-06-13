"""U0 — calibration metrics, fixtures, gate config (chart-UV plan §4)."""

import numpy as np

from chart_uv_agent.fixtures import (
    build_capsule_with_spikes, build_displaced_sphere, build_humanoid_blob,
)
from chart_uv_agent.gate import ChartGateConfig, evaluate_chart_gate
from uv_agent.geometry.evaluation import (
    boundary_straightness_score, uv_islands_from_uvmap,
)
from uv_agent.geometry.solution import UVMap


# -- fixtures (chart-UV plan §4.3) ------------------------------------------


def _is_closed_manifold(mesh) -> bool:
    return all(len(e.face_ids) == 2 for e in mesh.edges)


def test_fixtures_are_closed_manifolds():
    for mesh in (build_displaced_sphere(), build_capsule_with_spikes(), build_humanoid_blob()):
        assert mesh.face_count > 0
        assert _is_closed_manifold(mesh)  # watertight: every edge has exactly 2 faces


def test_capsule_spikes_create_extremities():
    plain = build_displaced_sphere(amp=0.0)  # round-ish
    spiky = build_capsule_with_spikes(n_spikes=4, spike_len=1.6)
    plain_extent = np.ptp([v.co for v in plain.vertices], axis=0).max()
    spiky_extent = np.ptp([v.co for v in spiky.vertices], axis=0).max()
    assert spiky_extent > plain_extent  # spikes push the bounding box out


def test_humanoid_blob_has_high_dihedral_creases():
    blob = build_humanoid_blob()
    creases = [e for e in blob.edges if e.dihedral_angle >= 60.0]
    assert len(creases) > 0  # limb junctions fold sharply


# -- uv_islands_from_uvmap (chart-UV plan U0) -------------------------------


def test_uv_islands_single_when_all_welded():
    mesh = build_displaced_sphere(segments=12, rings=8)
    uvmap = UVMap.for_mesh(mesh)  # all-zero -> every shared edge welded
    islands = uv_islands_from_uvmap(mesh, uvmap)
    assert len(islands) == 1
    assert sum(len(i) for i in islands) == mesh.face_count


def test_uv_islands_split_when_uvs_differ_per_face():
    mesh = build_displaced_sphere(segments=12, rings=8)
    uvmap = UVMap.for_mesh(mesh)
    # Give every face a unique UV offset -> no two faces weld -> island per face.
    for f in mesh.faces:
        for li in f.loop_indices:
            uvmap.set(li, f.id * 10.0, f.id * 10.0)
    islands = uv_islands_from_uvmap(mesh, uvmap)
    assert len(islands) == mesh.face_count


# -- boundary straightness (chart-UV plan U1.5) -----------------------------


def test_boundary_straightness_straight_line_scores_high():
    # A straight chain of vertices: seam edges along a row of a grid plane.
    from uv_agent.io.fixtures import build_grid_plane
    plane = build_grid_plane(nx=5, ny=5)
    # Pick a straight run of boundary edges along one row (collinear vertices).
    straight = [e.id for e in plane.edges
                if abs(plane.vertex_co(e.vertex_ids[0])[1] - plane.vertex_co(e.vertex_ids[1])[1]) < 1e-9
                and abs(plane.vertex_co(e.vertex_ids[0])[1] - (-0.5)) < 1e-9]
    score = boundary_straightness_score(plane, straight)
    assert score["straightness"] > 0.95  # collinear -> ~0 turning


# -- gate config (chart-UV plan §2, calibrated U0) --------------------------

GOOD = {
    "overlap_ratio": 0.0005, "raster_overlap_ratio": 0.001, "stretch_score": 0.35, "packing_efficiency": 0.74,
    "island_count": 38, "small_island_ratio": 0.1, "vt_v_ratio": 1.4,
    "texel_density_variance": 0.5, "uv_bounds_ok": True, "fallback_used": False,
    "convexity_mean": 0.78, "convexity_p10": 0.6, "boundary_smoothness_mean": 1.4, "tendril_count": 0,
}


def _g(**ov):
    return evaluate_chart_gate({**GOOD, **ov}, config=ChartGateConfig())


def test_calibrated_bars_pinned():
    cfg = ChartGateConfig()
    assert cfg.stretch_max == 0.50      # max(0.5, smart_uv 0.116 x 1.5)
    # Recalibrated: Blender auto-packs the reference's OWN charts to only 0.62 (the
    # artist's 0.76 is manual), so ≥0.70 is unreachable; 0.50 is the auto-floor.
    assert cfg.packing_min == 0.42  # SLIM floor (§5d correctness)
    assert cfg.island_count_max == 60


def test_artist_style_layout_passes():
    assert _g().passed


def test_stretch_and_packing_are_hard():
    assert "stretch_score" in [c.name for c in _g(stretch_score=1.5).failures]
    assert "packing_efficiency" in [c.name for c in _g(packing_efficiency=0.4).failures]


def test_fallback_and_overlap_and_bounds_hard():
    assert "fallback_used" in [c.name for c in _g(fallback_used=True).failures]
    assert "overlap_ratio" in [c.name for c in _g(overlap_ratio=0.05).failures]
    assert "uv_bounds" in [c.name for c in _g(uv_bounds_ok=False).failures]
