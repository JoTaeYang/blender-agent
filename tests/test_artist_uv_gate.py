"""Artist gate + report + pipeline JSON schema — Blender-free unit tests
(AUTO_ARTIST_UV_PLAN §6/§7). The SLIM unwrap is the Blender step; the gate, report and
output schema are pure and tested here. A flat panel gives a clean, injective UV (no
flips), so it exercises a full HARD+QUALITY pass without Blender."""

import json

import numpy as np

from artist_uv_agent.classification import classify_parts
from artist_uv_agent.density import density_report, density_weights
from artist_uv_agent.descriptors import describe_parts
from artist_uv_agent.gate import (
    ArtistGateConfig, artist_report, evaluate_artist_gate,
)
from artist_uv_agent.pipeline import (
    build_artist_layout_json, build_artist_parts_json, compute_artist_metrics,
)
from artist_uv_agent.seams import part_seams
from artist_uv_agent.segmentation import segment_parts
from chart_uv_agent.fixtures import build_humanoid_blob
from uv_agent.io.fixtures import build_grid_plane


# -- gate tiers --------------------------------------------------------------

def test_hard_failure_blocks_quality_does_not():
    # raster overlap (hard) fails → verdict failed.
    bad = {"raster_overlap_ratio": 0.5, "uv_bounds_ok": True, "fallback_used": False,
           "overlap_ratio": 0.0, "texel_density_variance": 0.0, "min_nondetail_island_faces": 10,
           "packing_efficiency": 0.9, "stretch_score": 0.1, "island_count": 5,
           "vt_v_ratio": 1.1, "tendril_count": 0, "convexity_p10": 0.9}
    r = evaluate_artist_gate(bad)
    assert not r.passed and "raster_overlap_ratio" in [c.name for c in r.hard_failures]

    # only stretch (quality) fails → verdict still accepted.
    poor_stretch = dict(bad)
    poor_stretch["raster_overlap_ratio"] = 0.0
    poor_stretch["stretch_score"] = 0.9
    r2 = evaluate_artist_gate(poor_stretch)
    assert r2.passed
    assert "stretch_score" in [c.name for c in r2.quality_failures]


def test_packing_is_hard():
    """Packing was promoted to HARD (out/artist_full/t5850 shipped 0.24 as 'accepted'):
    a tile-wasting layout must FAIL, not pass."""
    m = {"raster_overlap_ratio": 0.0, "uv_bounds_ok": True, "fallback_used": False,
         "overlap_ratio": 0.0, "texel_density_variance": 0.0, "min_nondetail_island_faces": 10,
         "packing_efficiency": 0.24, "stretch_score": 0.1, "island_count": 5,
         "vt_v_ratio": 1.1, "tendril_count": 0, "convexity_p10": 0.9}
    r = evaluate_artist_gate(m)
    assert not r.passed
    assert "packing_efficiency" in [c.name for c in r.hard_failures]


def test_cylinder_blob_is_hard():
    """A cylinder that stays a blob/fragment (the trident complaint) is a HARD fail."""
    m = {"raster_overlap_ratio": 0.0, "uv_bounds_ok": True, "fallback_used": False,
         "overlap_ratio": 0.0, "texel_density_variance": 0.0, "min_nondetail_island_faces": 10,
         "packing_efficiency": 0.6, "stretch_score": 0.1, "island_count": 5,
         "vt_v_ratio": 1.1, "tendril_count": 0, "convexity_p10": 0.9, "cylinder_blob_count": 1}
    r = evaluate_artist_gate(m)
    assert not r.passed
    assert "cylinder_rectangular" in [c.name for c in r.hard_failures]


def test_min_island_size_is_hard():
    m = {"raster_overlap_ratio": 0.0, "uv_bounds_ok": True, "fallback_used": False,
         "overlap_ratio": 0.0, "texel_density_variance": 0.0, "min_nondetail_island_faces": 2,
         "packing_efficiency": 0.9, "stretch_score": 0.1, "island_count": 5,
         "vt_v_ratio": 1.1, "tendril_count": 0, "convexity_p10": 0.9}
    r = evaluate_artist_gate(m)
    assert "min_island_size" in [c.name for c in r.hard_failures]


def test_fallback_used_is_hard():
    m = {"raster_overlap_ratio": 0.0, "uv_bounds_ok": True, "fallback_used": True,
         "overlap_ratio": 0.0, "texel_density_variance": 0.0, "min_nondetail_island_faces": 10,
         "packing_efficiency": 0.9, "stretch_score": 0.1, "island_count": 5,
         "vt_v_ratio": 1.1, "tendril_count": 0, "convexity_p10": 0.9}
    assert not evaluate_artist_gate(m).passed


def test_gate_config_packing_floor_below_chart():
    """Artist packing floor is lower than the chart engine's 0.42 (readability first)."""
    assert ArtistGateConfig().packing_min < 0.42


# -- full pass on a clean panel ---------------------------------------------

def _identity_uv(mesh):
    from uv_agent.geometry.solution import UVMap
    uv = UVMap.for_mesh(mesh)
    co = np.array([v.co for v in mesh.vertices])
    lo, span = co[:, :2].min(0), (co[:, :2].max(0) - co[:, :2].min(0))
    for lp in mesh.loops:
        x, y, _ = mesh.vertices[lp.vertex_id].co
        uv.set(lp.index, (x - lo[0]) / span[0], (y - lo[1]) / span[1])
    return uv


def test_clean_panel_passes_all_gates():
    """A flat panel mapped to fill the tile (1 island, no distortion) passes every HARD
    and QUALITY gate — including the now-HARD packing floor."""
    mesh = build_grid_plane(nx=8, ny=8)
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    nbrs = {p.part_id: p.neighbors for p in seg.parts}
    cls = classify_parts(descs, nbrs)
    seam = part_seams(mesh, seg, descs, cls)
    m = compute_artist_metrics(mesh, _identity_uv(mesh), seam, cls)
    assert m["packing_efficiency"] >= 0.40
    r = evaluate_artist_gate(m)
    assert r.passed, [c.name for c in r.hard_failures]
    assert not r.quality_failures, [c.name for c in r.quality_failures]


# -- output schema -----------------------------------------------------------

def test_parts_json_schema_and_serializable():
    mesh = build_humanoid_blob()
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    nbrs = {p.part_id: p.neighbors for p in seg.parts}
    cls = classify_parts(descs, nbrs)
    seam = part_seams(mesh, seg, descs, cls)
    pj = build_artist_parts_json(seg.parts, descs, cls, seam, seg.history)
    json.dumps(pj)                                    # must be serialisable
    assert pj["engine"] == "artist"
    assert pj["part_count"] == len(seg.parts)
    for row in pj["parts"]:
        for key in ("part_id", "type", "face_count", "area", "segmentation_confidence",
                    "symmetry_mate", "chart_ids"):
            assert key in row


def test_layout_json_schema():
    """``artist_layout.json`` carries REPORT-ONLY metadata (intended grouping + measured
    orientation), never forced per-chart transforms — the final layout is the CONCAVE pack."""
    from artist_uv_agent.layout import layout_metadata
    mesh = build_humanoid_blob()
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    nbrs = {p.part_id: p.neighbors for p in seg.parts}
    cls = classify_parts(descs, nbrs)
    seam = part_seams(mesh, seg, descs, cls)
    w = density_weights(descs, cls)
    lmeta, per_part_density = layout_metadata(mesh, _fake(mesh, seam), seam, descs, cls, nbrs)
    rep = artist_report(lmeta, seam, cls, density_report(per_part_density, w))
    lj = build_artist_layout_json(lmeta, rep)
    json.dumps(lj)
    assert lj["engine"] == "artist"
    assert "layout" in lj and "report" in lj
    assert "intended_grouping" in lj["layout"]            # grouping is metadata, not forced
    assert "orientation_consistency" in lj["report"]
    assert "report-only" in lj["report"]["note"]


# -- cylinder rectangularity audit -------------------------------------------

def _long_cylinder(seg=20, h=14, rad=0.5, height=12.0):
    import math
    verts = [(rad * math.cos(2 * math.pi * s / seg), rad * math.sin(2 * math.pi * s / seg),
              k / h * height) for k in range(h + 1) for s in range(seg)]
    faces = [(k * seg + s, k * seg + (s + 1) % seg, (k + 1) * seg + (s + 1) % seg, (k + 1) * seg + s)
             for k in range(h) for s in range(seg)]
    from uv_agent.geometry.mesh_graph import MeshGraph
    return MeshGraph.from_faces("longcyl", verts, faces)


def test_cylinder_quality_flags_blob_not_rectangle():
    """A long cylinder unwrapped as a RECTANGLE (arc-length × height) passes; the SAME
    cylinder projected to its cross-section (a square blob) is flagged."""
    import math
    from uv_agent.geometry.solution import UVMap
    from artist_uv_agent.pipeline import cylinder_quality

    mesh = _long_cylinder()
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    nbrs = {p.part_id: p.neighbors for p in seg.parts}
    cls = classify_parts(descs, nbrs)
    assert any(c.type == "cylinder" for c in cls)
    seam = part_seams(mesh, seg, descs, cls)

    rect = UVMap.for_mesh(mesh)                       # arc-length (u) × height (v) → rectangle
    blob = UVMap.for_mesh(mesh)                       # cross-section projection → square blob
    rad = 0.5
    for lp in mesh.loops:
        x, y, z = mesh.vertices[lp.vertex_id].co
        rect.set(lp.index, (math.atan2(y, x) + math.pi) * rad, z)
        blob.set(lp.index, x, y)
    n_rect, _ = cylinder_quality(mesh, rect, seam, descs, cls)
    n_blob, det_blob = cylinder_quality(mesh, blob, seam, descs, cls)
    assert n_rect == 0
    assert n_blob >= 1 and any(d["blob"] for d in det_blob)


def test_island_aspect_fill():
    from artist_uv_agent.pipeline import _island_aspect_fill
    from uv_agent.geometry.mesh_graph import MeshGraph
    from uv_agent.geometry.solution import UVMap
    nx, ny, w, h = 8, 2, 8.0, 1.0                    # an explicit 8:1 rectangle
    verts = [(i / nx * w, j / ny * h, 0.0) for j in range(ny + 1) for i in range(nx + 1)]
    faces = [(j * (nx + 1) + i, j * (nx + 1) + i + 1,
              (j + 1) * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i)
             for j in range(ny) for i in range(nx)]
    mesh = MeshGraph.from_faces("rect", verts, faces)
    uv = UVMap.for_mesh(mesh)
    for lp in mesh.loops:
        x, y, _ = mesh.vertices[lp.vertex_id].co
        uv.set(lp.index, x, y)
    aspect, fill = _island_aspect_fill(mesh, [f.id for f in mesh.faces], uv)
    assert aspect > 3.0 and fill > 0.9              # a clean rectangle fills its bbox


def _fake(mesh, seam):
    from chart_uv_agent.segmentation import flood_charts
    from uv_agent.geometry.solution import UVMap
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
