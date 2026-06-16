"""A4 seam templates — Blender-free unit tests (AUTO_ARTIST_UV_PLAN §5.A4 / §AR4).

Validates the pure seam-set production: charts correspond to parts, every chart is a
seam-aware UV disk, panels stay intact, cylinders open lengthwise, and the chart→part
map is well defined. The SLIM unwrap that consumes these seams is the Blender step."""

import math

from artist_uv_agent.classification import classify_parts
from artist_uv_agent.descriptors import describe_parts
from artist_uv_agent.seams import part_seams, uv_is_disk
from artist_uv_agent.segmentation import part_seam_edges, segment_parts
from chart_uv_agent.fixtures import build_humanoid_blob
from chart_uv_agent.segmentation import flood_charts
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_grid_plane


def _cylinder(seg=20, h=12):
    verts = [(math.cos(2 * math.pi * s / seg), math.sin(2 * math.pi * s / seg), k / h * 6.0)
             for k in range(h + 1) for s in range(seg)]
    faces = [(k * seg + s, k * seg + (s + 1) % seg, (k + 1) * seg + (s + 1) % seg, (k + 1) * seg + s)
             for k in range(h) for s in range(seg)]
    return MeshGraph.from_faces("cyl", verts, faces)


def _run(mesh):
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    cls = classify_parts(descs, {p.part_id: p.neighbors for p in seg.parts})
    return seg, part_seams(mesh, seg, descs, cls)


# -- seam-aware disk test ----------------------------------------------------

def test_uv_is_disk_plane_and_tube():
    plane = build_grid_plane(nx=4, ny=4)
    assert uv_is_disk(plane, [f.id for f in plane.faces], set())
    tube = _cylinder(seg=12, h=6)
    faces = [f.id for f in tube.faces]
    assert not uv_is_disk(tube, faces, set())           # uncut tube is an annulus (χ=0)


def test_uv_is_disk_tube_opened_by_slit():
    """A lengthwise slit that does NOT disconnect the tube still opens it to a disk —
    the seam-aware test must see that (a raw euler count would not)."""
    from artist_uv_agent.descriptors import describe_parts
    from artist_uv_agent.seams import _open_tube_seam
    tube = _cylinder(seg=16, h=8)
    seg = segment_parts(tube)
    descs = describe_parts(tube, seg)
    faces = seg.parts[0].face_ids
    seams = part_seam_edges(tube, seg.face_part)
    slit = _open_tube_seam(tube, faces, descs[0], seams, None)
    assert slit, "expected a longitudinal slit"
    assert uv_is_disk(tube, faces, seams | set(slit))


# -- templates ---------------------------------------------------------------

def test_panel_stays_one_chart():
    seg, res = _run(build_grid_plane(nx=8, ny=8))
    assert len(res.chart_to_part) == 1
    assert res.chart_role[0] == "panel"
    assert not res.repair_log                # nothing to cut


def test_cylinder_opens_lengthwise():
    mesh = _cylinder()
    seg, res = _run(mesh)
    # an interior seam beyond the part-boundary floor was added (the lengthwise open cut)
    assert len(res.seams) > len(part_seam_edges(mesh, seg.face_part))
    assert any(o["op"] == "cylinder_template" for o in res.repair_log)


def _capped_cylinder(seg=16, h=8):
    """An open tube with a triangle-fan CAP closing the top end (a centre vertex), so the
    cylinder template must separate the cap and open the body lengthwise."""
    verts = [(math.cos(2 * math.pi * s / seg), math.sin(2 * math.pi * s / seg), k / h * 6.0)
             for k in range(h + 1) for s in range(seg)]
    faces = [(k * seg + s, k * seg + (s + 1) % seg, (k + 1) * seg + (s + 1) % seg, (k + 1) * seg + s)
             for k in range(h) for s in range(seg)]
    top = len(verts)
    verts.append((0.0, 0.0, 6.0))                       # cap centre
    ring = h * seg
    faces += [(ring + s, ring + (s + 1) % seg, top) for s in range(seg)]
    return MeshGraph.from_faces("capped_cyl", verts, faces)


def test_cylinder_template_separates_cap_and_opens_body():
    """A capped tube → a cap chart + a rectangular body chart (no blob). The body's UV
    island is a topological disk that flattens to a rectangle."""
    mesh = _capped_cylinder()
    seg, res = _run(mesh)
    assert any(o["op"] == "cylinder_template" and o["caps"] >= 1 for o in res.repair_log)
    assert "cap" in res.chart_role.values()
    for fs in flood_charts(mesh, res.seams):
        assert uv_is_disk(mesh, fs, res.seams)


def test_every_chart_is_a_uv_disk():
    for build in (lambda: build_grid_plane(nx=8, ny=8), _cylinder, build_humanoid_blob):
        mesh = build()
        seg, res = _run(mesh)
        for fs in flood_charts(mesh, res.seams):
            assert uv_is_disk(mesh, fs, res.seams)


def test_chart_to_part_is_well_defined():
    mesh = build_humanoid_blob()
    seg, res = _run(mesh)
    charts = flood_charts(mesh, res.seams)
    assert len(res.chart_to_part) == len(charts)
    for cid, fs in enumerate(charts):
        pid = res.chart_to_part[cid]
        # every face of a chart belongs to the chart's part (parts are seam-bounded)
        assert all(seg.face_part[f] == pid for f in fs)
        assert res.chart_role[cid] == next(c.type for c in
            classify_parts(describe_parts(mesh, seg), {p.part_id: p.neighbors for p in seg.parts})
            if c.part_id == pid)


def test_part_charts_partition_charts():
    mesh = build_humanoid_blob()
    seg, res = _run(mesh)
    all_charts = sorted(c for cs in res.part_charts.values() for c in cs)
    assert all_charts == sorted(res.chart_to_part)
    assert not res.cap_exceeded
