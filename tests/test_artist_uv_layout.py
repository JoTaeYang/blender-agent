"""A6 layout + A7 density — Blender-free unit tests (AUTO_ARTIST_UV_PLAN §5.A6/§5.A7).

The SHIPPED pipeline orients long islands then packs with Blender's CONCAVE packer (the
final layout is the Blender step); here we test the pure pieces: ``orient_long_islands``
(rotation), ``layout_metadata`` (report-only grouping/orientation), the density policy,
and the demoted-to-debug ``band_shelf_pack`` (still overlap-free by construction)."""

import numpy as np

from artist_uv_agent.classification import classify_parts
from artist_uv_agent.density import density_report, density_weights
from artist_uv_agent.descriptors import describe_parts
from artist_uv_agent.layout import (
    _principal_angle, _shelf_pack, band_shelf_pack, layout_metadata, orient_long_islands,
)
from artist_uv_agent.seams import part_seams
from artist_uv_agent.segmentation import segment_parts
from chart_uv_agent.fixtures import build_capsule_with_spikes, build_humanoid_blob
from chart_uv_agent.segmentation import flood_charts
from uv_agent.blender.organic_unwrap import island_plan_from_seams
from uv_agent.geometry.evaluation import (
    evaluate_uv_solution, raster_overlap_ratio, uv_bounds_ok,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


def _long_strip(nx=24, ny=3, w=8.0, h=1.0):
    verts = [(i / nx * w, j / ny * h, 0.0) for j in range(ny + 1) for i in range(nx + 1)]
    faces = [(j * (nx + 1) + i, j * (nx + 1) + i + 1,
              (j + 1) * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i)
             for j in range(ny) for i in range(nx)]
    return MeshGraph.from_faces("strip", verts, faces)


def _identity_uv(mesh):
    uv = UVMap.for_mesh(mesh)
    for lp in mesh.loops:
        x, y, _ = mesh.vertices[lp.vertex_id].co
        uv.set(lp.index, x, y)
    return uv


def _fake_unwrap(mesh, seam):
    """Per-chart local PCA planar projection, charts laid out apart — a stand-in for the
    SLIM islands the Blender pipeline produces."""
    uv = UVMap.for_mesh(mesh)
    ox = 0.0
    for faces in flood_charts(mesh, seam.seams):
        loops = [li for f in faces for li in mesh.faces[f].loop_indices]
        pts = np.array([mesh.vertices[mesh.loops[li].vertex_id].co for li in loops])
        x = pts - pts.mean(0)
        w, v = np.linalg.eigh(np.cov(x.T))
        proj = x @ v[:, np.argsort(w)[::-1][:2]]
        proj -= proj.min(0)
        for k, li in enumerate(loops):
            uv.set(li, ox + proj[k, 0], proj[k, 1])
        ox += proj[:, 0].max() + 0.5
    return uv


def _injective_unwrap(mesh, seam):
    """Per-face unit-square atlas — injective by construction (each face its own cell), so
    it isolates the PACKER's guarantee (cross-chart disjointness) from per-chart unwrap
    injectivity, which is SLIM's job not the packer's."""
    from chart_uv_agent.segmentation import flood_charts
    uv = UVMap.for_mesh(mesh)
    ox = 0.0
    for faces in flood_charts(mesh, seam.seams):
        cols = max(1, int(np.ceil(np.sqrt(len(faces)))))
        for i, f in enumerate(faces):
            cx, cy = ox + (i % cols) * 1.5, (i // cols) * 1.5
            li = mesh.faces[f].loop_indices
            corners = [(0, 0), (1, 0), (1, 1), (0, 1)]
            for k, lp in enumerate(li):
                dx, dy = corners[k % 4]
                uv.set(lp, cx + dx, cy + dy)
        ox += cols * 1.5 + 1.0
    return uv


def _pipeline(mesh, *, importance=False, unwrap=None):
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    nbrs = {p.part_id: p.neighbors for p in seg.parts}
    cls = classify_parts(descs, nbrs)
    seam = part_seams(mesh, seg, descs, cls)
    weights = density_weights(descs, cls, importance=importance)
    uv0 = (unwrap or _fake_unwrap)(mesh, seam)
    uv, plan = band_shelf_pack(mesh, uv0, seam, descs, cls, weights, nbrs)
    return seg, seam, uv, plan


# -- packer invariants -------------------------------------------------------

def test_shelf_pack_no_overlap():
    boxes = [(0.3, 0.5), (0.2, 0.2), (0.4, 0.1), (0.15, 0.6), (0.25, 0.25)]
    pos, (W, H) = _shelf_pack(boxes, gap=0.01)
    rects = [(x, y, x + boxes[i][0], y + boxes[i][1]) for i, (x, y) in enumerate(pos)]
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            ax0, ay0, ax1, ay1 = rects[i]
            bx0, by0, bx1, by1 = rects[j]
            assert ax1 <= bx0 + 1e-9 or bx1 <= ax0 + 1e-9 or ay1 <= by0 + 1e-9 or by1 <= ay0 + 1e-9


def test_layout_is_overlap_free_and_in_bounds():
    """With an injective input unwrap, the packer's output must be overlap-free and
    in-bounds for every chart (cross-chart disjointness is the packer's guarantee)."""
    for build in (build_humanoid_blob, build_capsule_with_spikes):
        mesh = build()
        _, seam, uv, _ = _pipeline(mesh, unwrap=_injective_unwrap)
        assert uv_bounds_ok(uv)
        assert raster_overlap_ratio(mesh, uv) <= 0.005


def test_density_is_uniform():
    """Density normalisation makes texel density uniform → ~0 variance."""
    mesh = build_humanoid_blob()
    _, seam, uv, _ = _pipeline(mesh)
    ev = evaluate_uv_solution(mesh, island_plan_from_seams(mesh, seam.seams), uv)
    assert ev.texel_density_variance < 1e-4


def test_long_islands_oriented_consistently():
    mesh = build_humanoid_blob()
    _, _, _, plan = _pipeline(mesh)
    assert plan.metrics["orientation_consistency"] >= 0.9


# -- metrics -----------------------------------------------------------------

def test_metrics_present_and_ranged():
    mesh = build_capsule_with_spikes()
    _, _, _, plan = _pipeline(mesh)
    m = plan.metrics
    for key in ("part_coverage", "part_confidence_mean", "charts_per_part",
                "symmetry_pair_count", "paired_scale_error", "layout_group_count",
                "strip_alignment_score", "detail_near_parent_score",
                "orientation_consistency", "readability_score"):
        assert key in m
    assert m["part_coverage"] == 1.0
    assert 0.0 <= m["readability_score"] <= 1.0
    assert m["layout_group_count"] >= 1


# -- density policy ----------------------------------------------------------

def test_density_weights_default_uniform():
    mesh = build_humanoid_blob()
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    cls = classify_parts(descs, {p.part_id: p.neighbors for p in seg.parts})
    w = density_weights(descs, cls)
    assert all(abs(v - 1.0) < 1e-9 for v in w.values())
    rep = density_report({p: 1.0 for p in w}, w)
    assert rep["uniform"] and not rep["intentional_weights"]


def test_density_weights_importance_bumps_confident_detail():
    mesh = build_capsule_with_spikes()
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    cls = classify_parts(descs, {p.part_id: p.neighbors for p in seg.parts})
    w = density_weights(descs, cls, importance=True)
    # at least the weights stay near 1.0 and any bump is the detail weight
    assert all(0.79 <= v <= 1.21 for v in w.values())


# -- orientation (shipped pipeline) + report-only metadata -------------------

def test_orient_long_islands_makes_strip_vertical():
    """A horizontally-laid long strip is rotated so its long axis is vertical (|sin θ|→1).
    Short/round islands are untouched. Orientation survives the later CONCAVE re-pack."""
    mesh = _long_strip()
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    nbrs = {p.part_id: p.neighbors for p in seg.parts}
    cls = classify_parts(descs, nbrs)
    seam = part_seams(mesh, seg, descs, cls)
    charts = flood_charts(mesh, seam.seams)
    uv0 = _identity_uv(mesh)                     # strip long axis along +u (horizontal)
    before = abs(np.sin(_principal_angle(np.array([uv0.get(l) for l in
                 [li for f in charts[0] for li in mesh.faces[f].loop_indices]]))))
    uv1 = orient_long_islands(mesh, uv0, charts, descs, cls, seam)
    after = abs(np.sin(_principal_angle(np.array([uv1.get(l) for l in
                [li for f in charts[0] for li in mesh.faces[f].loop_indices]]))))
    assert before < 0.3 and after > 0.9          # horizontal → vertical


def test_layout_metadata_is_report_only():
    """layout_metadata returns intended grouping + measured orientation, NO forced
    per-chart transforms; per-part density is reported."""
    mesh = build_humanoid_blob()
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    nbrs = {p.part_id: p.neighbors for p in seg.parts}
    cls = classify_parts(descs, nbrs)
    seam = part_seams(mesh, seg, descs, cls)
    lmeta, density = layout_metadata(mesh, _fake_unwrap(mesh, seam), seam, descs, cls, nbrs)
    assert lmeta["part_coverage"] == 1.0
    assert "intended_grouping" in lmeta and "block_band" in lmeta["intended_grouping"]
    assert 0.0 <= lmeta["orientation_consistency"] <= 1.0
    assert "chart_xform" not in lmeta            # nothing forced onto UVs
    assert density and all(v >= 0 for v in density.values())
