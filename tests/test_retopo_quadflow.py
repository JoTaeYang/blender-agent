"""Quad-flow scoring + improvement (retopology plan §6.6, §6.8, §10 Phase 6)."""

from retopo_agent.geometry.decimate import decimate_to_target
from retopo_agent.geometry.features import feature_vertex_mask
from retopo_agent.geometry.quadflow import (
    improve_quad_flow,
    quad_flow_score,
    relax_vertices,
    tris_to_quads,
    vertex_valence,
)
from retopo_agent.io.fixtures import build_uv_sphere
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_cube, build_grid_plane


def _triangulate(mesh: MeshGraph) -> MeshGraph:
    faces = []
    for f in mesh.faces:
        v = f.vertex_ids
        for k in range(1, len(v) - 1):
            faces.append([v[0], v[k], v[k + 1]])
    return MeshGraph.from_faces(mesh.object_id + "_tri", [vv.co for vv in mesh.vertices], faces)


# -- scoring ---------------------------------------------------------------


def test_perfect_grid_plane_scores_high():
    plane = build_grid_plane(6, 6)  # all squares, interior valence 4
    rep = quad_flow_score(plane)
    assert rep.quad_fraction == 1.0
    assert rep.face_squareness > 0.99
    assert rep.valence_regularity == 1.0  # every interior vertex is valence 4
    assert rep.score > 0.95


def test_triangulated_grid_scores_lower_than_quad():
    plane = build_grid_plane(6, 6)
    quad_score = quad_flow_score(plane).score
    tri_score = quad_flow_score(_triangulate(plane)).score
    assert tri_score < quad_score
    assert quad_flow_score(_triangulate(plane)).quad_fraction == 0.0


def test_valence_histogram_and_issues():
    plane = build_grid_plane(4, 4)
    rep = quad_flow_score(plane)
    # interior 3x3 vertices all valence 4 -> no issues
    assert rep.valence_issue_count == 0
    assert rep.valence_histogram == {4: 9}


# -- tris -> quads ---------------------------------------------------------


def test_tris_to_quads_recovers_quads():
    plane = build_grid_plane(6, 6)
    tri = _triangulate(plane)  # 72 triangles
    quadded = tris_to_quads(tri, max_angle=40.0)
    assert quadded.face_count < tri.face_count  # pairs merged
    after = quad_flow_score(quadded)
    assert after.quad_fraction > 0.9  # almost everything back to quads
    assert after.ngon_count == 0


def test_tris_to_quads_keeps_vertices_and_no_ngons():
    sph = build_uv_sphere(segments=20, rings=12)
    tri = _triangulate(sph)
    quadded = tris_to_quads(tri, max_angle=60.0)
    assert quadded.vertex_count == sph.vertex_count  # vertex set preserved
    assert all(len(f.vertex_ids) <= 4 for f in quadded.faces)  # only tris/quads


# -- relax -----------------------------------------------------------------


def test_relax_pins_features_and_boundary():
    plane = build_grid_plane(6, 6)
    mask = feature_vertex_mask(plane, angle_threshold=30.0)  # boundary verts
    relaxed = relax_vertices(plane, iterations=5, feature_mask=mask)
    # Corner (a pinned boundary vertex) must not move.
    corner_before = plane.vertices[0].co
    corner_after = relaxed.vertices[0].co
    assert max(abs(a - b) for a, b in zip(corner_before, corner_after)) < 1e-9


def test_relax_smooths_a_noisy_grid():
    plane = build_grid_plane(8, 8)
    # Perturb interior vertices off-plane; relax should pull them back flat.
    coords = []
    for vid, v in enumerate(plane.vertices):
        x, y, z = v.co
        on_boundary = abs(x) >= 0.499 or abs(y) >= 0.499
        coords.append((x, y, z if on_boundary else z + (0.05 if vid % 2 else -0.05)))
    noisy = MeshGraph.from_faces("noisy", coords, [f.vertex_ids for f in plane.faces])
    before = max(abs(v.co[2]) for v in noisy.vertices)
    relaxed = relax_vertices(noisy, iterations=20)
    after = max(abs(v.co[2]) for v in relaxed.vertices)
    assert after < before  # smoothed toward the plane


# -- end-to-end: completion criterion --------------------------------------


def test_improve_quad_flow_beats_raw_decimate():
    # "more natural quad flow than a simple decimate result" (plan §10 Phase 6).
    high = build_uv_sphere(segments=48, rings=32)
    decimated = decimate_to_target(high, 600).low_mesh
    before = quad_flow_score(decimated)

    mask = feature_vertex_mask(decimated, angle_threshold=45.0)
    improved = improve_quad_flow(decimated, feature_mask=mask, max_angle=60.0, relax_iterations=6)
    after = quad_flow_score(improved)

    assert after.score > before.score
    assert after.quad_fraction >= before.quad_fraction
    assert improved.face_count <= decimated.face_count  # merging tris reduces count
