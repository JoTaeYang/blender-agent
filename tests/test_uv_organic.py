"""Organic UV unwrap — Tracks 1 & 2 + the P5 gate (UV repair plan §3/§5).

Pure, Blender-free: the seam strategy selection, cut-tree disk-opening, crease
selection, small-island merge, refinement-path generation, the new evaluation
metrics, and the hard/soft gate. The Blender unwrap itself is exercised by the
headless worker P5-resume runs.
"""

import numpy as np

from retopo_agent.io.fixtures import build_subdivided_cube, build_uv_sphere
from uv_agent.geometry.evaluation import (
    estimate_vt_count, evaluate_uv_solution, per_face_stretch, uv_bounds_ok,
)
from uv_agent.geometry.solution import UVMap
from uv_agent.geometry.uv_gate import (
    UVGateThresholds, UVReferenceBaseline, evaluate_uv_gate,
)
from uv_agent.planner.island_planner import plan_islands
from uv_agent.planner.organic_seams import (
    classify_seam_strategy, crease_seam_edges, edge_over_threshold_fraction,
    merge_small_islands, organic_seam_edges, refinement_seam_edges,
)


def _islands(mesh, seams):
    return plan_islands(mesh, angle_threshold=1e9, split_by_material=False,
                        forced_seam_ids=set(seams)).islands


# -- Track 1: strategy + cut-tree (plan §3) ---------------------------------


def test_strategy_classifier_thresholds():
    cube = build_subdivided_cube(divisions=8)
    assert 0.0 <= edge_over_threshold_fraction(cube, 30.0) <= 1.0
    # The over-threshold fraction is monotonically non-increasing in the threshold.
    assert edge_over_threshold_fraction(cube, 1.0) >= edge_over_threshold_fraction(cube, 90.0)
    # A flat-faceted subdivided cube has few >30° edges -> hard_surface.
    assert classify_seam_strategy(cube, angle_threshold=30.0) == "hard_surface"
    # A curved sphere: nearly every edge has some dihedral -> organic at a tiny cut.
    sphere = build_uv_sphere(rings=10, segments=16)
    assert classify_seam_strategy(sphere, angle_threshold=0.5) == "organic"


def test_cut_tree_opens_closed_surface_to_single_disk():
    # Cutting a closed genus-0 surface along the cut-TREE yields exactly one island
    # (a disk/pelt) per connected component — the core Track 1 property.
    for mesh in (build_uv_sphere(rings=8, segments=12), build_subdivided_cube(divisions=6)):
        seams = organic_seam_edges(mesh, n_extremities=6)
        islands = [i for i in _islands(mesh, seams) if i.face_ids]
        assert len(islands) == 1


def test_crease_seams_are_a_subset_above_threshold():
    cube = build_subdivided_cube(divisions=6)
    creases = crease_seam_edges(cube, percentile=50, min_angle=10.0)
    for eid in creases:
        e = cube.edges[eid]
        assert len(e.face_ids) == 2 and e.dihedral_angle >= 10.0


def test_crease_seams_increase_island_count_over_tree():
    sphere = build_uv_sphere(rings=10, segments=16)
    tree = organic_seam_edges(sphere, n_extremities=6)
    withcr = organic_seam_edges(sphere, n_extremities=6, crease_percentile=80)
    assert len(withcr) >= len(tree)


# -- Track 2: merge + refinement (plan §3) ----------------------------------


def test_merge_small_islands_reduces_count():
    sphere = build_uv_sphere(rings=12, segments=18)
    over = organic_seam_edges(sphere, n_extremities=6, crease_percentile=70)
    before = len([i for i in _islands(sphere, over) if i.face_ids])
    merged = merge_small_islands(sphere, over, min_island_faces=10, max_islands=8)
    after = len([i for i in _islands(sphere, merged) if i.face_ids])
    assert after <= before
    assert merged.issubset(over)  # merging only ever REMOVES seam edges


def test_merge_never_removes_boundary_edges():
    # A grid plane is open: its border edges are boundaries and must stay seams.
    from uv_agent.io.fixtures import build_grid_plane
    plane = build_grid_plane(nx=5, ny=5)
    boundary = {e.id for e in plane.edges if e.is_boundary}
    merged = merge_small_islands(plane, set(boundary), min_island_faces=999, max_islands=1)
    assert boundary.issubset(merged)


def test_refinement_returns_interior_edges_of_island():
    sphere = build_uv_sphere(rings=10, segments=16)
    seams = organic_seam_edges(sphere, n_extremities=6)
    island = [i for i in _islands(sphere, seams) if i.face_ids][0]
    fstretch = np.ones(sphere.face_count)
    new_edges = refinement_seam_edges(sphere, island.face_ids, seams, fstretch)
    assert all(eid not in seams for eid in new_edges)  # genuinely new cuts


# -- evaluation metrics (plan §3 Track 2 / §5) ------------------------------


def test_per_face_stretch_near_zero_for_isometric_map():
    from uv_agent.io.fixtures import build_grid_plane
    plane = build_grid_plane(nx=4, ny=4, size=1.0)
    # UV == the flat XY position is an isometry (up to a global scale) -> ~0 stretch.
    uvmap = UVMap.for_mesh(plane)
    for loop in plane.loops:
        x, y, _z = plane.vertices[loop.vertex_id].co
        uvmap.set(loop.index, x, y)
    pfs = per_face_stretch(plane, uvmap)
    assert pfs.shape == (plane.face_count,)
    assert float(np.abs(pfs).max()) < 1e-6


def test_estimate_vt_count_counts_seam_splits():
    cube = build_subdivided_cube(divisions=2)
    uvmap = UVMap.for_mesh(cube)
    # All UVs identical -> every vertex collapses to one (vertex, uv) corner.
    assert estimate_vt_count(cube, uvmap) == cube.vertex_count


def test_uv_bounds_ok():
    cube = build_subdivided_cube(divisions=2)
    uvmap = UVMap.for_mesh(cube)
    assert uv_bounds_ok(uvmap) is True
    uvmap.set(0, 2.0, 2.0)
    assert uv_bounds_ok(uvmap) is False


# -- P5 gate: hard vs soft, fallback hard (plan §5) -------------------------

BASE = UVReferenceBaseline(stretch_score=0.19, vt_v_ratio=1.13, island_count=12)

GOOD = {
    "overlap_ratio": 0.0004, "island_count": 6, "small_island_ratio": 0.1,
    "vt_v_ratio": 1.19, "stretch_score": 0.20, "packing_efficiency": 0.7,
    "uv_bounds_ok": True, "fallback_used": False,
}


def _gate(**ov):
    return evaluate_uv_gate({**GOOD, **ov}, baseline=BASE, thresholds=UVGateThresholds())


def test_clean_layout_passes():
    g = _gate()
    assert g.passed and g.verdict == "accepted"
    assert not g.hard_failures and not g.soft_failures


def test_fallback_used_is_a_hard_failure():
    g = _gate(fallback_used=True)
    assert g.passed is False
    assert "fallback_used" in [c.name for c in g.hard_failures]


def test_overlap_and_island_count_and_vt_are_hard():
    assert "overlap_ratio" in [c.name for c in _gate(overlap_ratio=0.05).hard_failures]
    assert "island_count" in [c.name for c in _gate(island_count=80).hard_failures]
    assert "vt_v_ratio" in [c.name for c in _gate(vt_v_ratio=2.5).hard_failures]
    assert "uv_bounds" in [c.name for c in _gate(uv_bounds_ok=False).hard_failures]


def test_stretch_and_packing_are_soft_not_blocking():
    g = _gate(stretch_score=1.5, packing_efficiency=0.3, small_island_ratio=0.4)
    assert g.passed is True  # hard gates still hold -> shippable
    soft = [c.name for c in g.soft_failures]
    assert "stretch_score" in soft and "packing_efficiency" in soft and "small_island_ratio" in soft


def test_stretch_gate_is_reference_relative():
    # Stretch within reference × 1.25 passes its (soft) check.
    g = _gate(stretch_score=0.19 * 1.25 - 0.01)
    assert all(c.passed for c in g.checks if c.name == "stretch_score")
