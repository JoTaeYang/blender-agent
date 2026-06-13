"""True (raster) UV overlap metric — correctness round. Pure, Blender-free.

Synthetic fixtures: a clean non-overlapping layout (→ 0, like the reference artist UVs
measured at 0.0 in Blender), a self-intersection (one chart folds onto itself), and an
inter-chart invasion (two charts overlap). Verifies the metric + the self/cross
attribution that drives the two different repairs.
"""

import math

from uv_agent.geometry.evaluation import raster_overlap_diagnosis, raster_overlap_ratio
from uv_agent.geometry.solution import UVMap
from uv_agent.io.fixtures import build_grid_plane

_CORNERS = [(0.12, 0.12), (0.88, 0.12), (0.88, 0.88), (0.12, 0.88)]


def _grid_layout(plane, overlap_face=None, overlap_cell=(0, 0)):
    """Lay each face in its own grid cell (no overlap); optionally place ``overlap_face``
    onto ``overlap_cell`` so it overlaps whoever else is there."""
    uv = UVMap.for_mesh(plane)
    cols = math.ceil(math.sqrt(plane.face_count))
    cell = 1.0 / cols
    for i, f in enumerate(plane.faces):
        cx, cy = (i % cols) * cell, (i // cols) * cell
        if overlap_face is not None and f.id == overlap_face:
            cx, cy = overlap_cell[0] * cell, overlap_cell[1] * cell
        for j, li in enumerate(f.loop_indices):
            u, v = _CORNERS[j % 4]
            uv.set(li, cx + u * cell, cy + v * cell)
    return uv


def test_clean_layout_is_zero():
    plane = build_grid_plane(nx=6, ny=6)
    uv = _grid_layout(plane)
    assert raster_overlap_ratio(plane, uv, resolution=1024) == 0.0  # margin erodes aliasing


def test_self_intersection_detected_and_attributed():
    plane = build_grid_plane(nx=6, ny=6)
    # Faces 0 and 1 are the SAME chart; put face 1 onto face 0's cell → self-overlap.
    uv = _grid_layout(plane, overlap_face=1, overlap_cell=(0, 0))
    fc = {f.id: (0 if f.id < 2 else 1 + f.id) for f in plane.faces}
    d = raster_overlap_diagnosis(plane, uv, fc, resolution=1024)
    assert d["raster_overlap_ratio"] > 0.005
    assert d["self_px"] > 0 and d["cross_px"] == 0  # one chart folds → self
    assert 0 in d["self_charts"] and d["cross_charts"] == []


def test_inter_chart_invasion_detected_and_attributed():
    plane = build_grid_plane(nx=6, ny=6)
    # Faces 0 and 1 are DIFFERENT charts; overlap them → cross invasion.
    uv = _grid_layout(plane, overlap_face=1, overlap_cell=(0, 0))
    fc = {f.id: f.id for f in plane.faces}  # every face its own chart
    d = raster_overlap_diagnosis(plane, uv, fc, resolution=1024)
    assert d["raster_overlap_ratio"] > 0.005
    assert d["cross_px"] > 0 and d["self_px"] == 0  # two charts overlap → cross
    assert set(d["cross_charts"]) >= {0, 1}


def test_unwrap_defaults_to_slim_for_correctness():
    # §5d: the unwrap must default to the locally-injective SLIM (MINIMUM_STRETCH) so
    # charts do not self-fold — split is the exception, never the driver.
    import inspect

    from chart_uv_agent.unwrap import reunwrap_faces, unwrap_and_pack
    assert inspect.signature(unwrap_and_pack).parameters["method"].default == "MINIMUM_STRETCH"
    assert inspect.signature(reunwrap_faces).parameters["method"].default == "MINIMUM_STRETCH"
    # And the separate non-injective minimize_stretch is OFF by default (would re-fold).
    assert inspect.signature(unwrap_and_pack).parameters["minimize_iters"].default == 0


def test_margin_erodes_boundary_aliasing():
    # Adjacent (non-overlapping) faces sharing an edge must NOT register as overlap.
    plane = build_grid_plane(nx=4, ny=4)
    uv = UVMap.for_mesh(plane)
    for f in plane.faces:                       # identity XY layout: faces tile, share edges
        for li in f.loop_indices:
            x, y, _ = plane.vertices[plane.loops[li].vertex_id].co
            uv.set(li, (x + 0.5), (y + 0.5))    # into [0,1]
    assert raster_overlap_ratio(plane, uv, resolution=1024) <= 0.005
