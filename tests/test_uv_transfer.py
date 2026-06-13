"""Reference-Guided UV Transfer — Blender-free unit tests (UV_TRANSFER_PLAN §5).

Covers T1 chart extraction, T2 projection guards (normal compatibility, speckle
smoothing, connected-component enforcement) and T4 placement math (scale/rotation/IoU),
plus the T5 gate. The Blender BVH/unwrap/placement integration is exercised by the
headless acceptance run, not here.
"""

import numpy as np

from transfer_uv_agent.gate import (
    TransferGateConfig, correspondence_report, evaluate_transfer_gate,
)
from transfer_uv_agent.placement import (
    _chart_loops, place_group_density_first, separate_charts,
)
from transfer_uv_agent.projection import (
    UNASSIGNED, build_brute_oracle, enforce_connected_components, fill_unassigned,
    pick_compatible_hit, project_chart_ids, smooth_labels,
)
from transfer_uv_agent.reference import extract_reference_charts
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.io.fixtures import build_grid_plane


def _two_island_uv(plane, *, mid=0.0):
    """UVMap with the plane split into two non-overlapping UV islands by centroid x:
    each island maps vertex (x,y) by its OWN affine, so within-island edges weld but the
    shared seam between the halves does not (→ exactly two islands)."""
    uv = UVMap.for_mesh(plane)
    left_faces = set()
    for f in plane.faces:
        cx = np.mean([plane.vertices[v].co[0] for v in f.vertex_ids])
        island_left = cx < mid
        if island_left:
            left_faces.add(f.id)
        for li in f.loop_indices:
            x, y, _ = plane.vertices[plane.loops[li].vertex_id].co
            u = (x + 0.5) * 0.4 + (0.05 if island_left else 0.55)
            v = (y + 0.5) * 0.4 + 0.05
            uv.set(li, u, v)
    return uv, left_faces


# -- T1: chart extraction ----------------------------------------------------

def test_extract_two_reference_charts():
    plane = build_grid_plane(nx=4, ny=2)
    uv, left = _two_island_uv(plane)
    charts = extract_reference_charts(plane, uv)
    assert len(charts) == 2
    # Each chart's center sits in its own UV half; footprints are non-empty.
    centers = sorted(c.center[0] for c in charts)
    assert centers[0] < 0.5 < centers[1]
    for c in charts:
        assert c.footprint.any()
        assert c.texel_density > 0
        assert c.half_extents[0] > 0 and c.half_extents[1] > 0


# -- T2: projection guards ---------------------------------------------------

def test_pick_compatible_hit_rejects_opposite_normal():
    # Nearest hit (dist 0.01) has an OPPOSITE normal → must skip to the compatible hit.
    cands = [(1, 0.01, (0, 0, -1)), (0, 0.05, (0, 0, 1))]
    assert pick_compatible_hit((0, 0, 1), cands, min_dot=0.2, max_distance=1.0) == 0
    # All compatible hits beyond max_distance → unassigned.
    far = [(0, 5.0, (0, 0, 1))]
    assert pick_compatible_hit((0, 0, 1), far, min_dot=0.2, max_distance=1.0) is None


def test_two_close_opposite_planes_do_not_bleed():
    # Reference = a top plane (+Z) and a near-coincident bottom plane (−Z, reversed
    # winding). An adaptive face on the top must take the top chart, never bleed to the
    # closer-or-equal opposite-normal bottom shell.
    top = build_grid_plane(nx=2, ny=2)
    verts = [v.co for v in top.vertices] + [(v.co[0], v.co[1], -0.02) for v in top.vertices]
    n = len(top.vertices)
    faces = [list(f.vertex_ids) for f in top.faces]  # +Z
    faces += [list(reversed([v + n for v in f.vertex_ids])) for f in top.faces]  # −Z, below
    ref = MeshGraph.from_faces("two_planes", verts, faces)
    ref_face_chart = {f.id: (0 if f.id < len(top.faces) else 1) for f in ref.faces}
    oracle = build_brute_oracle(ref, ref_face_chart)

    label = project_chart_ids(top, oracle, min_dot=0.2, max_distance=1.0)
    assert set(label.values()) == {0}  # every top face → top chart, no bleed


def test_smooth_labels_fixes_speckle():
    plane = build_grid_plane(nx=5, ny=5)
    label = {f.id: 0 for f in plane.faces}
    speck = plane.faces[12].id            # an interior face
    label[speck] = 7                       # a lone speckle surrounded by 0s
    out = smooth_labels(plane, label, rounds=10)
    assert out[speck] == 0


def test_enforce_connected_components_absorbs_minor_and_splits_major():
    plane = build_grid_plane(nx=6, ny=2)   # 12 faces in a strip
    # id 0 on the whole strip, except a single far-flung face also labelled... a fresh id.
    label = {f.id: 0 for f in plane.faces}
    # Inject a disconnected fragment with the same id 0 is impossible (it'd be connected);
    # instead give a contiguous block id 1 and a lone disconnected face id 1.
    for fid in (0, 1, 2, 3, 4):
        label[fid] = 1                     # major component of id 1
    label[11] = 1                          # lone disconnected face of id 1 (minor)
    out, log = enforce_connected_components(plane, label, minor_frac=0.2)
    # The lone disconnected id-1 fragment (face 11) is absorbed away — id 1 is no longer
    # split across two components.
    assert out[11] != 1
    # INVARIANT: every surviving id is now exactly one connected component.
    from transfer_uv_agent.projection import _components
    adj = plane.face_adjacency()
    by_id = {}
    for f, c in out.items():
        by_id.setdefault(c, set()).add(f)
    for c, fs in by_id.items():
        assert len(_components(plane, fs, adj)) == 1, f"id {c} is disconnected"


def test_enforce_keeps_major_split_as_fresh_id():
    # Two genuinely large same-id components (mirrored limbs) → the smaller keeps a FRESH
    # id inheriting the same reference slot, not absorbed.
    plane = build_grid_plane(nx=7, ny=2)   # 14 faces
    label = {f.id: 9 for f in plane.faces}  # background id
    for fid in (0, 1, 7, 8):               # left block, id 3
        label[fid] = 3
    for fid in (5, 6, 12, 13):             # right block, also id 3 (disconnected)
        label[fid] = 3
    out, log = enforce_connected_components(plane, label, minor_frac=0.2)
    assert any(s["ref_id"] == 3 for s in log)   # a fresh id inheriting ref slot 3
    from transfer_uv_agent.projection import _components
    adj = plane.face_adjacency()
    by_id = {}
    for f, c in out.items():
        by_id.setdefault(c, set()).add(f)
    for c, fs in by_id.items():
        assert len(_components(plane, fs, adj)) == 1


def test_fill_unassigned_from_neighbours():
    plane = build_grid_plane(nx=3, ny=3)
    label = {f.id: 0 for f in plane.faces}
    label[4] = UNASSIGNED                  # center face unassigned
    out = fill_unassigned(plane, label)
    assert out[4] == 0


# -- T4: density-first placement ---------------------------------------------

def test_density_first_translates_to_slot_without_rescaling():
    # Reference chart at slot center (0.7,0.3).
    refp = build_grid_plane(nx=3, ny=3)
    ref_uv = UVMap.for_mesh(refp)
    for f in refp.faces:
        for li in f.loop_indices:
            x, y, _ = refp.vertices[refp.loops[li].vertex_id].co
            ref_uv.set(li, 0.6 + (x + 0.5) * 0.2, 0.2 + (y + 0.5) * 0.2)
    ref_chart = extract_reference_charts(refp, ref_uv)[0]

    adp = build_grid_plane(nx=3, ny=3)
    uv = UVMap.for_mesh(adp)
    for f in adp.faces:
        for li in f.loop_indices:
            x, y, _ = adp.vertices[adp.loops[li].vertex_id].co
            uv.set(li, x * 0.2 + 0.1, y * 0.2 + 0.1)   # some uniform-density layout
    faces = [f.id for f in adp.faces]
    loops = _chart_loops(adp, faces)
    area_before = uv.uv[loops].max(axis=0) - uv.uv[loops].min(axis=0)

    place_group_density_first(adp, faces, uv, ref_chart)
    pts = uv.uv[loops]
    center = (pts.min(axis=0) + pts.max(axis=0)) / 2.0
    # Centroid moved to the slot center...
    assert abs(center[0] - ref_chart.center[0]) < 1e-3
    assert abs(center[1] - ref_chart.center[1]) < 1e-3
    # ...and the chart was NOT rescaled (density-first: rotation+translation only).
    area_after = pts.max(axis=0) - pts.min(axis=0)
    assert abs(np.prod(area_after) - np.prod(area_before)) < 1e-6


def test_separate_charts_removes_bbox_overlap():
    # Two identical charts placed on top of each other → separation pushes them apart.
    plane = build_grid_plane(nx=2, ny=2)
    uv = UVMap.for_mesh(plane)
    for f in plane.faces:
        for li in f.loop_indices:
            x, y, _ = plane.vertices[plane.loops[li].vertex_id].co
            uv.set(li, 0.5 + x * 0.1, 0.5 + y * 0.1)
    half = [f.id for f in plane.faces[:2]]
    other = [f.id for f in plane.faces[2:]]
    charts = [(0, half), (1, other)]   # both occupy the same UV box
    moved = separate_charts(plane, uv, charts, passes=60)
    assert moved > 0
    la = uv.uv[_chart_loops(plane, half)]
    lb = uv.uv[_chart_loops(plane, other)]
    # Their bboxes no longer overlap on at least one axis.
    sep_x = la[:, 0].max() <= lb[:, 0].min() + 1e-9 or lb[:, 0].max() <= la[:, 0].min() + 1e-9
    sep_y = la[:, 1].max() <= lb[:, 1].min() + 1e-9 or lb[:, 1].max() <= la[:, 1].min() + 1e-9
    assert sep_x or sep_y


# -- T5: gate ----------------------------------------------------------------

def _gate(**m):
    base = {"raster_overlap_ratio": 0.001, "overlap_ratio": 0.0, "texel_density_variance": 0.001,
            "packing_efficiency": 0.6, "uv_bounds_ok": True, "fallback_used": False}
    return evaluate_transfer_gate({**base, **m}, config=TransferGateConfig())


def test_gate_passes_clean_and_blocks_overlap_and_fallback():
    assert _gate().passed
    assert not _gate(raster_overlap_ratio=0.02).passed       # overlapping UV breaks baking
    assert not _gate(fallback_used=True).passed              # Smart-UV may not ship
    assert not _gate(uv_bounds_ok=False).passed


def test_gate_blocks_nonuniform_texel_density():
    # The density-first hard gate: the old 1.2 regression must FAIL, uniform (~0) passes.
    assert not _gate(texel_density_variance=1.2).passed
    assert _gate(texel_density_variance=0.0001).passed
    assert TransferGateConfig().texel_density_variance_max == 0.62  # ref 0.515 × 1.2


def test_gate_blocks_collapsed_packing():
    # Gate parity (round 3): the chart engine gates packing >= 0.50; round 2 shipped a
    # 0.029-packing layout because this gate was silently absent here. Never again.
    assert not _gate(packing_efficiency=0.029).passed
    assert not _gate(packing_efficiency=0.49).passed
    assert _gate(packing_efficiency=0.51).passed
    assert TransferGateConfig().packing_min == 0.50


def test_correspondence_report_lists_uncovered():
    class C:  # minimal ref chart stand-in
        def __init__(self, cid):
            self.chart_id = cid
    refs = [C(0), C(1), C(2)]
    placements = [type("P", (), {"iou": 0.5})(), type("P", (), {"iou": 0.7})()]
    rep = correspondence_report(refs, {0: 0, 1: 1}, placements, ref_count=3)
    assert rep["uncovered_reference_charts"] == [2]
    assert rep["adaptive_chart_count"] == 2
    assert rep["mean_placement_iou"] == 0.6
