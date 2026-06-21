"""Editor edge geometry export (Electron MVP 2 plan §5, Session A).

The single largest production risk in the MVP 2 seam editor is the UI selecting a
different edge than the one the Blender worker means (plan §5, §14 "UI edge id와
Blender edge id 불일치"). The mitigation: the worker exports the mesh's edge table
*with its Blender edge ids* and the renderer uses **only** ``edges[].id`` as the
selectable id — it never re-derives ids from an imported GLTF/OBJ ordering.

This module is the **pure** half of that export: it turns a
:class:`~uv_agent.geometry.mesh_graph.MeshGraph` into the canonical
``edge_geometry.json`` document (plan §5.1) and computes the mesh signature. It
imports no ``bpy``/``bmesh`` so it is unit-testable from the synthetic fixtures,
and — crucially — it consumes the *same* :class:`MeshGraph` that
:func:`uv_agent.blender.extract.extract_mesh_graph` produces, whose edge ids are
the bmesh ``edge.index`` values. That guarantees the exported ``edges[].id`` is
identical to the id :mod:`artist_uv_agent.user_seams` validates a spec against.

Determinism: ``MeshGraph.vertices`` / ``.edges`` / ``.faces`` are id-indexed
lists, so the output arrays are already in ascending-id order with no sorting
needed; re-exporting the same mesh yields byte-identical JSON.
"""

from __future__ import annotations

from uv_agent.geometry.mesh_graph import MeshGraph

EDGE_GEOMETRY_SCHEMA_VERSION = 1

# Above this edge count the JSON grows past a few MB; the worker surfaces a
# warning so the renderer can show a "large mesh" hint (plan §11 Session A
# "large mesh JSON size warning") rather than silently shipping a huge payload.
LARGE_MESH_EDGE_WARN = 250_000


def mesh_signature(mesh: MeshGraph) -> dict:
    """The element-count fingerprint used to detect topology drift (plan §5).

    A renderer holding edge geometry / a seam spec must refuse to apply it if the
    mesh it was authored against no longer matches (plan §14 "mesh signature
    mismatch가 있으면 spec apply/save를 막는다"). Counts are a cheap, stable proxy.
    """
    return {
        "vertices": mesh.vertex_count,
        "edges": mesh.edge_count,
        "faces": mesh.face_count,
        "loops": len(mesh.loops),
    }


def build_edge_geometry(mesh: MeshGraph) -> dict:
    """Serialize ``mesh`` into the canonical ``edge_geometry.json`` (plan §5.1).

    ``edges[].id`` is the only selectable id the renderer may use; ``vertices[].co``
    are the 3D positions the renderer draws line segments from. Faces carry their
    ``edge_ids`` so the renderer can (optionally) shade or filter by face, but they
    are not selectable in MVP 2.
    """
    return {
        "schema_version": EDGE_GEOMETRY_SCHEMA_VERSION,
        "object": mesh.object_id,
        "vertices": [
            {"id": v.id, "co": [float(v.co[0]), float(v.co[1]), float(v.co[2])]}
            for v in mesh.vertices
        ],
        "edges": [
            {
                "id": e.id,
                "vertex_ids": [int(e.vertex_ids[0]), int(e.vertex_ids[1])],
                "face_ids": list(e.face_ids),
                "is_boundary": bool(e.is_boundary),
                "is_non_manifold": bool(e.is_non_manifold),
                "is_sharp": bool(e.is_sharp),
                "is_seam": bool(e.is_seam),
                "dihedral_angle": round(float(e.dihedral_angle), 4),
            }
            for e in mesh.edges
        ],
        "faces": [
            {
                "id": f.id,
                "vertex_ids": list(f.vertex_ids),
                "edge_ids": list(f.edge_ids),
                "material_index": int(f.material_index),
            }
            for f in mesh.faces
        ],
    }


def edge_geometry_size_warnings(mesh: MeshGraph) -> list[str]:
    """Best-effort warnings for an export that may be large (plan §11 Session A)."""
    warnings: list[str] = []
    if mesh.edge_count > LARGE_MESH_EDGE_WARN:
        warnings.append(
            f"large mesh: {mesh.edge_count} edges (> {LARGE_MESH_EDGE_WARN}); "
            "edge_geometry.json may be several MB and selection may be slow"
        )
    return warnings
