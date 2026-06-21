"""Edge geometry export contract tests (Electron MVP 2 plan §5, Session A).

Blender-free: builds a :class:`MeshGraph` from the synthetic fixtures (the same
graph type :func:`uv_agent.blender.extract.extract_mesh_graph` produces) and
asserts :func:`uv_agent.geometry.edge_geometry.build_edge_geometry` emits the
canonical ``edge_geometry.json`` with stable, mesh-aligned edge ids.
"""

import json

from chart_uv_agent.fixtures import build_folded_planes
from uv_agent.geometry.edge_geometry import (
    EDGE_GEOMETRY_SCHEMA_VERSION,
    build_edge_geometry,
    edge_geometry_size_warnings,
    mesh_signature,
)


def test_edge_geometry_shape_and_counts():
    mesh = build_folded_planes(n=4)
    geo = build_edge_geometry(mesh)

    assert geo["schema_version"] == EDGE_GEOMETRY_SCHEMA_VERSION
    assert geo["object"] == mesh.object_id
    # Edge / vertex / face counts equal the mesh's (plan §11 Session A acceptance:
    # "edge count가 Blender mesh edge count와 일치한다").
    assert len(geo["edges"]) == mesh.edge_count
    assert len(geo["vertices"]) == mesh.vertex_count
    assert len(geo["faces"]) == mesh.face_count


def test_edge_ids_are_dense_and_match_mesh_graph():
    """``edges[].id`` is the MeshGraph edge id — the only id the renderer may use
    and the same id ``UserSeamSpec`` is validated against (plan §5, §14)."""
    mesh = build_folded_planes(n=5)
    geo = build_edge_geometry(mesh)

    # Ids are dense 0..N-1 in array order (deterministic, no sorting needed).
    assert [e["id"] for e in geo["edges"]] == list(range(mesh.edge_count))
    assert [v["id"] for v in geo["vertices"]] == list(range(mesh.vertex_count))

    # Every exported edge's vertex pair resolves back to its own id via the mesh's
    # edge index — i.e. the id genuinely identifies that topological edge.
    for e in geo["edges"]:
        a, b = e["vertex_ids"]
        assert mesh.edge_key(a, b) == e["id"]


def test_edge_flags_and_dihedral_present():
    mesh = build_folded_planes(n=3)
    geo = build_edge_geometry(mesh)
    sample = geo["edges"][0]
    for key in ("id", "vertex_ids", "face_ids", "is_boundary",
                "is_non_manifold", "is_sharp", "is_seam", "dihedral_angle"):
        assert key in sample, key
    # The 90° fold edges exist and are interior (two faces) with ~90° dihedral.
    folds = [e for e in geo["edges"] if abs(e["dihedral_angle"] - 90.0) < 1.0]
    assert folds, "expected at least one ~90° fold edge in folded planes"
    assert all(len(e["face_ids"]) == 2 for e in folds)
    # Grid borders are mesh-boundary edges (single face).
    assert any(e["is_boundary"] and len(e["face_ids"]) == 1 for e in geo["edges"])


def test_export_is_deterministic():
    mesh = build_folded_planes(n=4)
    a = json.dumps(build_edge_geometry(mesh), sort_keys=False)
    b = json.dumps(build_edge_geometry(mesh), sort_keys=False)
    assert a == b  # byte-identical re-export (plan §11 "JSON schema가 deterministic")


def test_mesh_signature_matches():
    mesh = build_folded_planes(n=4)
    sig = mesh_signature(mesh)
    assert sig == {
        "vertices": mesh.vertex_count,
        "edges": mesh.edge_count,
        "faces": mesh.face_count,
        "loops": len(mesh.loops),
    }


def test_size_warning_only_for_large_mesh():
    mesh = build_folded_planes(n=4)
    assert edge_geometry_size_warnings(mesh) == []  # small fixture, no warning
