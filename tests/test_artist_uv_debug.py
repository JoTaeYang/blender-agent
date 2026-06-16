"""Debug overlays — Blender-free unit tests (AUTO_ARTIST_UV_PLAN §7/§12)."""

import numpy as np

from artist_uv_agent.classification import classify_parts
from artist_uv_agent.debug import (
    part_color, part_debug_rows, parts_uv_svg, rasterize_parts,
)
from artist_uv_agent.descriptors import describe_parts
from artist_uv_agent.seams import part_seams
from artist_uv_agent.segmentation import segment_parts
from chart_uv_agent.fixtures import build_humanoid_blob
from chart_uv_agent.segmentation import flood_charts
from uv_agent.geometry.solution import UVMap


def _setup(mesh):
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    nbrs = {p.part_id: p.neighbors for p in seg.parts}
    cls = classify_parts(descs, nbrs)
    seam = part_seams(mesh, seg, descs, cls)
    charts = flood_charts(mesh, seam.seams)
    uv = UVMap.for_mesh(mesh)
    ox = 0.0
    for faces in charts:
        loops = [li for f in faces for li in mesh.faces[f].loop_indices]
        pts = np.array([mesh.vertices[mesh.loops[li].vertex_id].co for li in loops])
        x = pts - pts.mean(0)
        w, v = np.linalg.eigh(np.cov(x.T))
        proj = x @ v[:, np.argsort(w)[::-1][:2]]
        proj -= proj.min(0)
        for k, li in enumerate(loops):
            uv.set(li, 0.1 + 0.02 * (ox + proj[k, 0]), 0.1 + 0.02 * proj[k, 1])
        ox += proj[:, 0].max() + 0.5
    return seg, descs, cls, seam, charts, uv


def test_part_color_deterministic_and_distinct():
    assert part_color(3) == part_color(3)
    assert part_color(0) != part_color(1)


def test_parts_uv_svg_is_valid():
    mesh = build_humanoid_blob()
    _, _, _, seam, charts, uv = _setup(mesh)
    svg = parts_uv_svg(mesh, uv, seam.chart_to_part, charts)
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert svg.count("<polygon") > 0


def test_rasterize_parts_paints_islands():
    mesh = build_humanoid_blob()
    _, _, _, seam, charts, uv = _setup(mesh)
    img = rasterize_parts(mesh, uv, seam.chart_to_part, charts, resolution=128)
    assert img.shape == (128, 128, 4)
    # some non-background pixels were painted
    painted = np.any(img[:, :, :3] > 0.2, axis=2)
    assert painted.sum() > 0


def test_part_debug_rows_schema():
    mesh = build_humanoid_blob()
    seg, descs, cls, seam, _, _ = _setup(mesh)
    rows = part_debug_rows(seg.parts, descs, cls, seam)
    assert len(rows) == len(seg.parts)
    for r in rows:
        assert set(r) >= {"part_id", "type", "face_count", "area", "chart_ids",
                          "symmetry_mate", "segmentation_confidence"}
