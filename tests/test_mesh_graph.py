import math

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io import fixtures


def test_cube_counts():
    c = fixtures.build_cube()
    assert (c.vertex_count, c.edge_count, c.face_count) == (8, 12, 6)


def test_cube_is_manifold_with_right_angle_dihedrals():
    c = fixtures.build_cube()
    assert all(not e.is_boundary for e in c.edges)
    assert all(not e.is_non_manifold for e in c.edges)
    assert all(len(e.face_ids) == 2 for e in c.edges)
    for e in c.edges:
        assert math.isclose(e.dihedral_angle, 90.0, abs_tol=1e-6)


def test_plane_boundary_edges():
    p = fixtures.build_grid_plane(4, 4)
    # 5x5 verts, perimeter has 16 boundary edges, interior is flat (dihedral 0).
    assert p.vertex_count == 25
    assert sum(e.is_boundary for e in p.edges) == 16
    assert max(e.dihedral_angle for e in p.edges) == 0.0


def test_face_normals_and_area():
    c = fixtures.build_cube()
    for f in c.faces:
        assert math.isclose(f.area_3d, 1.0, abs_tol=1e-6)  # unit cube faces
        assert math.isclose(sum(n * n for n in f.normal), 1.0, abs_tol=1e-6)


def test_face_adjacency_symmetry():
    c = fixtures.build_cube()
    adj = c.face_adjacency()
    for fid, neighbors in adj.items():
        for nb, edge_id in neighbors:
            assert (fid, edge_id) in [(x[0], x[1]) for x in [(fid, e) for _, e in adj[nb]]] or any(
                n == fid for n, _ in adj[nb]
            )


def test_json_roundtrip():
    c = fixtures.build_cube()
    d = c.to_dict()
    assert d["vertex_count"] == 8 and d["face_count"] == 6
    c2 = MeshGraph.from_dict(d)
    assert (c2.vertex_count, c2.edge_count, c2.face_count) == (8, 12, 6)


def test_cylinder_open_has_boundary():
    cyl = fixtures.build_cylinder(12, 3)
    # Open tube: top and bottom rings (12 + 12) are boundary edges.
    assert sum(e.is_boundary for e in cyl.edges) == 24
