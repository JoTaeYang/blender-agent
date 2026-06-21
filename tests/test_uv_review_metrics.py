"""Tests for the read-only UV review metrics (plan §6, Session A).

Pure Python: synthetic meshes + hand-authored UV maps, no Blender. The acceptance
criteria from the plan are checked directly — loop UV count matches the mesh,
bounds/island-count are deterministic, and a no-UV object is handled without an
exception.
"""

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.geometry.uv_review import compute_uv_review, packing_efficiency, uv_bounds


def _set_face_uv(mesh: MeshGraph, uvmap: UVMap, face_id: int, uvs):
    """Assign per-corner UVs (in face loop order) to a face."""
    for li, uv in zip(mesh.faces[face_id].loop_indices, uvs):
        uvmap.set(li, uv[0], uv[1])


def _single_quad():
    mesh = MeshGraph.from_faces(
        "quad",
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        [[0, 1, 2, 3]],
    )
    uvm = UVMap.for_mesh(mesh)
    _set_face_uv(mesh, uvm, 0, [(0, 0), (1, 0), (1, 1), (0, 1)])
    return mesh, uvm


def _two_quad_strip():
    """Two quads sharing the middle edge (verts 1 and 4)."""
    mesh = MeshGraph.from_faces(
        "strip",
        [(0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 1, 0), (1, 1, 0), (2, 1, 0)],
        [[0, 1, 4, 3], [1, 2, 5, 4]],
    )
    return mesh


def test_single_quad_is_clean():
    mesh, uvm = _single_quad()
    rep = compute_uv_review(mesh, uvm, raster_resolution=128)
    m = rep["metrics"]
    assert rep["uv"]["island_count"] == 1
    assert rep["uv"]["uv_bounds"]["in_0_1"] is True
    assert rep["uv"]["has_negative_uv"] is False
    assert rep["uv"]["has_out_of_tile_uv"] is False
    assert m["overlap_ratio"] == 0.0
    assert m["raster_overlap_ratio"] == 0.0
    assert m["stretch_score"] < 1e-6
    # A unit quad filling the unit square packs perfectly.
    assert m["packing_efficiency"] > 0.99


def test_welded_uv_is_one_island_split_is_two():
    mesh = _two_quad_strip()
    # Welded: shared verts (1, 4) carry the same UV across the seam edge.
    welded = UVMap.for_mesh(mesh)
    _set_face_uv(mesh, welded, 0, [(0, 0), (0.5, 0), (0.5, 1), (0, 1)])
    _set_face_uv(mesh, welded, 1, [(0.5, 0), (1, 0), (1, 1), (0.5, 1)])
    assert compute_uv_review(mesh, welded, raster_resolution=128)["uv"]["island_count"] == 1

    # Split: face 1 is shifted so the shared verts get different UVs -> 2 islands.
    split = UVMap.for_mesh(mesh)
    _set_face_uv(mesh, split, 0, [(0, 0), (0.4, 0), (0.4, 1), (0, 1)])
    _set_face_uv(mesh, split, 1, [(0.6, 0), (1.0, 0), (1.0, 1), (0.6, 1)])
    assert compute_uv_review(mesh, split, raster_resolution=128)["uv"]["island_count"] == 2


def test_flipped_face_reports_overlap():
    mesh, uvm = _single_quad()
    # Reverse the winding so the signed UV area is negative (a fold).
    _set_face_uv(mesh, uvm, 0, [(0, 1), (1, 1), (1, 0), (0, 0)])
    m = compute_uv_review(mesh, uvm, raster_resolution=128)["metrics"]
    assert m["overlap_ratio"] > 0.5


def test_out_of_tile_uv_flagged():
    mesh, uvm = _single_quad()
    _set_face_uv(mesh, uvm, 0, [(0, 0), (1.5, 0), (1.5, 1.2), (0, 1.2)])
    rep = compute_uv_review(mesh, uvm, raster_resolution=128)
    assert rep["uv"]["has_out_of_tile_uv"] is True
    assert rep["uv"]["uv_bounds"]["in_0_1"] is False
    assert rep["uv"]["has_negative_uv"] is False


def test_negative_uv_flagged():
    mesh, uvm = _single_quad()
    _set_face_uv(mesh, uvm, 0, [(-0.2, 0), (1, 0), (1, 1), (-0.2, 1)])
    rep = compute_uv_review(mesh, uvm, raster_resolution=128)
    assert rep["uv"]["has_negative_uv"] is True
    assert rep["uv"]["has_out_of_tile_uv"] is True


def test_two_charts_on_same_square_raster_overlap():
    # Two disconnected quads both mapped onto the full unit square -> they overlap.
    mesh = MeshGraph.from_faces(
        "two",
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
         (5, 0, 0), (6, 0, 0), (6, 1, 0), (5, 1, 0)],
        [[0, 1, 2, 3], [4, 5, 6, 7]],
    )
    uvm = UVMap.for_mesh(mesh)
    _set_face_uv(mesh, uvm, 0, [(0, 0), (1, 0), (1, 1), (0, 1)])
    _set_face_uv(mesh, uvm, 1, [(0, 0), (1, 0), (1, 1), (0, 1)])
    rep = compute_uv_review(mesh, uvm, raster_resolution=128)
    assert rep["uv"]["island_count"] == 2
    m = rep["metrics"]
    assert m["raster_overlap_ratio"] > 0.5
    # Two different charts invade the same pixels -> cross, not self.
    assert m["cross_overlap_ratio"] > 0.0


def test_loop_uv_count_matches_mesh_loops():
    # Plan Session A acceptance: a UVMap covers exactly the mesh loop count.
    mesh = _two_quad_strip()
    uvm = UVMap.for_mesh(mesh)
    assert len(uvm.uv) == len(mesh.loops)


def test_packing_efficiency_half_used():
    mesh, uvm = _single_quad()
    # Map the quad into the bottom-left quarter -> uses 1/4 of the bbox... but the
    # bbox is the island extent itself, so a single tight island still packs ~1.
    _set_face_uv(mesh, uvm, 0, [(0, 0), (0.5, 0), (0.5, 0.5), (0, 0.5)])
    assert packing_efficiency(mesh, uvm) > 0.99


def test_uv_bounds_empty_uvmap():
    mesh = _two_quad_strip()
    empty = UVMap.for_mesh(mesh)  # all zeros
    b = uv_bounds(empty)
    assert b["in_0_1"] is True
    assert b["min"] == [0.0, 0.0]


def test_deterministic():
    mesh, uvm = _single_quad()
    a = compute_uv_review(mesh, uvm, raster_resolution=128)
    b = compute_uv_review(mesh, uvm, raster_resolution=128)
    assert a == b
