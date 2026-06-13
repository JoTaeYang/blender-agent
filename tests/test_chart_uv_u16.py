"""U1.6 — chart shape repair (chart-UV plan §5b). Pure, Blender-free."""

import numpy as np

from chart_uv_agent.fixtures import build_displaced_sphere, build_humanoid_blob
from chart_uv_agent.segmentation import (
    flood_charts, is_disk, mandatory_seam_edges, segment,
)
from chart_uv_agent.shape import (
    chart_convexity, measure_charts, tendril_chains,
)
from chart_uv_agent.shape_repair import geometric_bisect, repair_shapes


def _connected(mesh, fs):
    adj = mesh.face_adjacency()
    s = set(fs)
    seen = {fs[0]}
    st = [fs[0]]
    while st:
        c = st.pop()
        for nb, _ in adj[c]:
            if nb in s and nb not in seen:
                seen.add(nb)
                st.append(nb)
    return len(seen) == len(fs)


# -- shape metrics ----------------------------------------------------------

def test_convexity_full_for_convex_disk_low_for_pocket():
    from uv_agent.io.fixtures import build_grid_plane
    plane = build_grid_plane(nx=5, ny=5)
    full = list(range(plane.face_count))
    assert chart_convexity(plane, full) > 0.95  # a filled rectangle is convex
    # Carve an L-shape (remove a quadrant) → concave → convexity drops.
    keep = [f for f in full
            if not (np.mean([plane.vertices[v].co[0] for v in plane.faces[f].vertex_ids]) > 0
                    and np.mean([plane.vertices[v].co[1] for v in plane.faces[f].vertex_ids]) > 0)]
    concave = chart_convexity(plane, keep)
    assert concave < 0.95 and concave < chart_convexity(plane, full)  # notch lowers convexity


def test_measure_charts_keys():
    mesh = build_displaced_sphere()
    seg = segment(mesh, cone_limit=120)
    mand = mandatory_seam_edges(mesh, fold_angle=90.0)
    m = measure_charts(mesh, list(seg.charts.values()), seg.seams, mand)
    for k in ("convexity_mean", "convexity_p10", "boundary_smoothness_mean", "tendril_count"):
        assert k in m


# -- geometric bisect (op 3 primitive) --------------------------------------

def test_geometric_bisect_yields_two_connected_disks():
    mesh = build_displaced_sphere()
    seams = set(mandatory_seam_edges(mesh))
    chart = max(flood_charts(mesh, seams), key=len)
    cf = set(chart)
    new = geometric_bisect(mesh, chart, seams)
    assert new
    pieces = [c for c in flood_charts(mesh, seams | set(new)) if set(c) & cf]
    assert len(pieces) == 2  # exactly two connected halves
    assert all(_connected(mesh, p) for p in pieces)
    # (Disk-ness of the halves is validated by repair_shapes, which rejects a split
    # whose halves are not disks — geometric_bisect alone need not guarantee it.)


# -- repair_shapes ----------------------------------------------------------

def test_repair_raises_convexity_and_keeps_disks():
    for mesh in (build_displaced_sphere(), build_humanoid_blob()):
        seg = segment(mesh, cone_limit=150)
        seams = set(seg.seams)
        before = float(np.mean([chart_convexity(mesh, c) for c in flood_charts(mesh, seams)]))
        repair_shapes(mesh, seams, convexity_min=0.92, max_charts=60)
        charts = flood_charts(mesh, seams)
        after = float(np.mean([chart_convexity(mesh, c) for c in charts]))
        assert after >= before - 1e-6                 # never regresses convexity
        assert all(is_disk(mesh, c) for c in charts)   # disk invariant preserved
        assert all(_connected(mesh, c) for c in charts)


def test_repair_only_adds_charts_never_recombines():
    mesh = build_displaced_sphere()
    seg = segment(mesh, cone_limit=150)
    seams = set(seg.seams)
    before = len(flood_charts(mesh, seams))
    repair_shapes(mesh, seams, convexity_min=0.92, max_charts=60)
    after = len(flood_charts(mesh, seams))
    assert after >= before  # concavity split only adds charts (no antagonistic merge)


def test_repair_preserves_mandatory_seams():
    mesh = build_humanoid_blob()
    seg = segment(mesh, cone_limit=150)
    seams = set(seg.seams)
    mand = mandatory_seam_edges(mesh, fold_angle=90.0)
    repair_shapes(mesh, seams, convexity_min=0.92, max_charts=60)
    assert mand.issubset(seams)  # R2 folds never crossed/removed


def test_repair_respects_chart_cap():
    mesh = build_displaced_sphere(segments=28, rings=20)
    seg = segment(mesh, cone_limit=150)
    seams = set(seg.seams)
    repair_shapes(mesh, seams, convexity_min=0.99, max_charts=30)
    assert len(flood_charts(mesh, seams)) <= 30 + 2  # cap honored (small overrun ok)
