"""Decimation pre-process topology diagnosis -- Phase DM2 (decimation plan §5).

These verify, offline (no Blender), the diagnosis core the plan's §5 completion
criterion calls for: connected components, tiny detached shells, boundary /
non-manifold edges, degeneracy / duplicates, area distribution, feature
boundaries, and the recommended component policy that feeds the retry ladder. The
Blender adapter (:mod:`retopo_agent.blender.diagnosis`) only wraps this on a mesh
extracted from a ``bpy`` object, so the logic itself is fully covered here.
"""

from retopo_agent.geometry.diagnosis import (
    POLICY_COMPONENT_BUDGET,
    POLICY_LARGEST_ONLY,
    POLICY_PRESERVE_ALL,
    diagnose_topology,
    recommend_component_policy,
)
from retopo_agent.io.fixtures import build_uv_sphere
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_cube, build_grid_plane, build_two_material_plane


def _combine(*meshes) -> MeshGraph:
    """Merge several MeshGraphs into one, offsetting vertex indices so each stays a
    separate vertex-connected component."""
    verts: list[tuple[float, float, float]] = []
    faces: list[list[int]] = []
    for m in meshes:
        off = len(verts)
        verts.extend(v.co for v in m.vertices)
        faces.extend([vid + off for vid in f.vertex_ids] for f in m.faces)
    return MeshGraph.from_faces("combined", verts, faces)


def _tiny_triangle(x: float) -> MeshGraph:
    """A standalone triangle near ``x`` on the X axis (its own component)."""
    verts = [(x, 0.0, 0.0), (x + 0.1, 0.0, 0.0), (x, 0.1, 0.0)]
    return MeshGraph.from_faces("tri", verts, [[0, 1, 2]])


# -- components ------------------------------------------------------------


def test_single_clean_component_recommends_preserve_all():
    rep = diagnose_topology(build_cube())
    assert rep.component_count == 1
    assert rep.largest_component_face_ratio == 1.0
    assert rep.tiny_component_count == 0
    assert rep.boundary_edge_count == 0
    assert rep.non_manifold_edge_count == 0
    assert rep.needs_cleanup is False
    assert rep.recommended_policy == POLICY_PRESERVE_ALL


def test_many_tiny_components_recommend_component_budget():
    # One dominant grid (100 quads) + several tiny one-face shells: the anchor's
    # 25-component / 20-tiny structure in miniature (plan §5).
    big = build_grid_plane(10, 10)  # 100 quads, one component
    tinies = [_tiny_triangle(5.0 + 2.0 * i) for i in range(5)]
    rep = diagnose_topology(_combine(big, *tinies))

    assert rep.component_count == 6  # 1 big + 5 tiny
    assert rep.tiny_component_count == 5
    assert rep.largest_component_face_count == 100
    assert 0.9 < rep.largest_component_face_ratio < 1.0
    assert 0.0 < rep.tiny_component_face_ratio < 0.1
    assert rep.recommended_policy == POLICY_COMPONENT_BUDGET


# -- edges / boundaries ----------------------------------------------------


def test_open_boundary_counted():
    rep = diagnose_topology(build_grid_plane(4, 4))
    assert rep.boundary_edge_count == 16  # 4 sides * 4 segments
    assert rep.non_manifold_edge_count == 0


def test_non_manifold_edge_detected_and_flags_cleanup():
    # Three quads sharing edge (0,1) -> that edge is non-manifold (cf. validator test).
    verts = [
        (0, 0, 0), (0, 1, 0),
        (1, 1, 0), (1, 0, 0),
        (-1, 1, 0), (-1, 0, 0),
        (0, 1, 1), (0, 0, 1),
    ]
    fan = MeshGraph.from_faces("fan", verts, [[0, 1, 2, 3], [0, 1, 4, 5], [0, 1, 6, 7]])
    rep = diagnose_topology(fan)
    assert rep.non_manifold_edge_count >= 1
    assert rep.needs_cleanup is True
    assert any("non-manifold" in r for r in rep.cleanup_reasons)


def test_material_and_sharp_boundaries_counted():
    rep = diagnose_topology(build_two_material_plane(4, 4))
    assert rep.material_boundary_edge_count > 0  # the slot split down the middle
    # A flat plane has no sharp dihedral edges, but its open border verts are sharp
    # only via boundary; interior dihedral is 0 so sharp_edge_count stays 0 here.
    assert rep.sharp_edge_count == 0


def test_sharp_edges_counted_on_cube():
    # Every cube edge is a 90 deg dihedral -> all 12 are sharp-normal boundaries.
    rep = diagnose_topology(build_cube())
    assert rep.sharp_edge_count == 12


# -- degeneracy / duplicates ----------------------------------------------


def test_degenerate_face_detected():
    # A collinear "triangle" has zero area; a real triangle gives a nonzero median.
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (2, 0, 0), (3, 0, 0)]
    mesh = MeshGraph.from_faces("deg", verts, [[0, 1, 2], [0, 3, 4]])  # 2nd is collinear
    rep = diagnose_topology(mesh)
    assert rep.degenerate_face_count == 1
    assert rep.needs_cleanup is True


def test_very_small_triangle_detected():
    # Two unit triangles (area 0.5) + one tiny triangle well under 1% of the median.
    verts = [
        (0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),  # big quad -> 2 tris
        (2, 0, 0), (2.01, 0, 0), (2, 0.001, 0),       # tiny triangle, area ~5e-6
    ]
    mesh = MeshGraph.from_faces("small", verts, [[0, 1, 2], [0, 2, 3], [4, 5, 6]])
    rep = diagnose_topology(mesh)
    assert rep.very_small_triangle_count == 1
    assert rep.degenerate_face_count == 0


def test_duplicate_face_and_vertex_detected():
    # Vertex 3 duplicates vertex 0's coordinate; face [0,1,2] appears twice.
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 0, 0)]
    mesh = MeshGraph.from_faces("dup", verts, [[0, 1, 2], [0, 1, 2]])
    rep = diagnose_topology(mesh)
    assert rep.duplicate_face_count == 1
    assert rep.duplicate_vertex_count == 1
    assert rep.near_duplicate_vertex_count >= 1
    assert rep.needs_cleanup is True


# -- area distribution -----------------------------------------------------


def test_face_area_distribution_fields():
    rep = diagnose_topology(build_uv_sphere(segments=20, rings=12))
    for key in ("min", "max", "mean", "median", "p10", "p90"):
        assert key in rep.face_area
    assert rep.face_area["max"] >= rep.face_area["median"] >= rep.face_area["min"]


# -- policy recommendation (used by the retry ladder, plan §5) -------------


def test_recommend_policy_branches():
    assert recommend_component_policy(1, 1.0, 0) == POLICY_PRESERVE_ALL
    # anchor-like: 25 components, 0.98 dominant, 20 tiny -> component budget.
    assert recommend_component_policy(25, 0.98, 20) == POLICY_COMPONENT_BUDGET
    # dominant shell + only negligible debris (no tiny by count rule) -> largest_only.
    assert recommend_component_policy(3, 0.99, 0) == POLICY_LARGEST_ONLY
    # fragmented with no dominant shell -> fall back to preserve_all.
    assert recommend_component_policy(5, 0.4, 3) == POLICY_PRESERVE_ALL


# -- output contract / robustness -----------------------------------------


def test_to_dict_has_plan_section5_contract():
    rep = diagnose_topology(build_cube()).to_dict()
    for key in (
        "component_count",
        "largest_component_face_ratio",
        "boundary_edge_count",
        "non_manifold_edge_count",
        "tiny_component_count",
        "recommended_policy",
    ):
        assert key in rep


def test_empty_mesh_is_safe():
    empty = MeshGraph.from_faces("empty", [], [])
    rep = diagnose_topology(empty)
    assert rep.component_count == 0
    assert rep.face_count == 0
    assert rep.recommended_policy == POLICY_PRESERVE_ALL
