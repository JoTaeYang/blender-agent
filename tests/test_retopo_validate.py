"""Topology validator (retopology plan §6.6, §10 Phase 2, §15.6).

Confirms the validator numerically reports the Phase 2 completion criteria --
"no n-gons", "quad ratio", "closeness to target polycount" -- plus non-manifold
detection, and reduces them to the accepted/retry/failed bands.
"""

from retopo_agent.geometry.validate import (
    count_face_types,
    validate_topology,
)
from retopo_agent.io.fixtures import build_uv_sphere
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_cube, build_grid_plane


def test_count_face_types_on_sphere():
    # sphere: segments*(rings-2) quads + 2*segments cap triangles, no n-gons
    sph = build_uv_sphere(segments=20, rings=12)
    tris, quads, ngons = count_face_types(sph)
    assert quads == 20 * (12 - 2)
    assert tris == 2 * 20
    assert ngons == 0


def test_pure_quad_closed_cube_accepted_on_target():
    cube = build_cube()  # 6 quads, closed manifold
    rep = validate_topology(cube, target_face_count=6)
    assert rep.ngon_count == 0
    assert rep.triangle_count == 0
    assert rep.quad_ratio == 1.0
    assert rep.non_manifold_edge_count == 0
    assert rep.open_boundary_count == 0
    assert rep.status == "accepted"


def test_ngon_detected_and_blocks_acceptance():
    # A single pentagon face -> one n-gon.
    verts = [(0, 0, 0), (1, 0, 0), (1.5, 1, 0), (0.5, 1.6, 0), (-0.5, 1, 0)]
    pent = MeshGraph.from_faces("pent", verts, [[0, 1, 2, 3, 4]])
    rep = validate_topology(pent, target_face_count=1, quad_required=False, expect_closed=False)
    assert rep.ngon_count == 1
    assert rep.status == "retry"  # §15.7: n-gon -> cleanup required, not accepted
    assert any("ngon_count" in r for r in rep.reasons)
    # Escalates to failed if it survived a cleanup pass (§15.6).
    rep2 = validate_topology(
        pent, target_face_count=1, quad_required=False, expect_closed=False, ngon_after_cleanup=True
    )
    assert rep2.status == "failed"


def test_ngon_allowed_does_not_block():
    verts = [(0, 0, 0), (1, 0, 0), (1.5, 1, 0), (0.5, 1.6, 0), (-0.5, 1, 0)]
    pent = MeshGraph.from_faces("pent", verts, [[0, 1, 2, 3, 4]])
    rep = validate_topology(
        pent, target_face_count=1, quad_required=False, ngon_allowed=True, expect_closed=False
    )
    assert rep.ngon_count == 1
    assert rep.status == "accepted"


def test_all_triangle_mesh_fails_quad_ratio():
    # Two triangles forming a quad region: quad_ratio 0 -> failed when quads required.
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    tri_mesh = MeshGraph.from_faces("tri", verts, [[0, 1, 2], [0, 2, 3]])
    rep = validate_topology(tri_mesh, target_face_count=2, quad_required=True, expect_closed=False)
    assert rep.triangle_count == 2
    assert rep.quad_ratio == 0.0
    assert rep.triangle_ratio == 1.0
    assert rep.status == "failed"  # quad_ratio < 0.90 and triangle_ratio > 0.10


def test_non_manifold_edge_detected():
    # Three quads sharing edge (0,1) -> that edge is non-manifold. All-quad, so
    # only the non-manifold metric (not quad/triangle ratio) drives the status.
    verts = [
        (0, 0, 0), (0, 1, 0),   # 0,1 shared edge
        (1, 1, 0), (1, 0, 0),   # quad A
        (-1, 1, 0), (-1, 0, 0),  # quad B
        (0, 1, 1), (0, 0, 1),   # quad C
    ]
    fan = MeshGraph.from_faces("fan", verts, [[0, 1, 2, 3], [0, 1, 4, 5], [0, 1, 6, 7]])
    rep = validate_topology(fan, target_face_count=3, quad_required=True, expect_closed=False)
    assert rep.non_manifold_edge_count >= 1
    assert rep.triangle_count == 0 and rep.ngon_count == 0
    assert rep.status == "retry"  # repairable non-manifold geometry
    assert any("non_manifold" in r for r in rep.reasons)


def test_open_boundary_gated_only_when_expect_closed():
    plane = build_grid_plane(4, 4)  # all quads, but an open border
    closed = validate_topology(plane, target_face_count=16, expect_closed=True)
    assert closed.open_boundary_count > 0
    assert closed.status == "retry"  # open border on an expected-closed mesh
    opened = validate_topology(plane, target_face_count=16, expect_closed=False)
    assert opened.status == "accepted"  # boundary not gated for an open asset


def test_target_error_drives_status():
    cube = build_cube()  # 6 faces
    # target 6 -> on target; target 100 -> face_count 6 is 94% short -> failed band
    assert validate_topology(cube, target_face_count=6).status == "accepted"
    far = validate_topology(cube, target_face_count=100)
    assert far.target_error_ratio > 0.30
    assert far.status == "failed"


def test_report_dict_has_phase2_fields():
    rep = validate_topology(build_cube(), target_face_count=6).to_dict()
    for key in (
        "face_count",
        "target_face_count",
        "quad_ratio",
        "triangle_count",
        "ngon_count",
        "non_manifold_edge_count",
        "status",
    ):
        assert key in rep
