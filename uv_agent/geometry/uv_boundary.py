"""Extract UV island boundaries as seam edges (Electron MVP 2 plan §6.4, Session B).

The most valuable MVP 2 shortcut: turn an *existing* UV layer's island boundaries
back into ``user_seam_edges`` so the artist starts from the model's current seams
instead of a blank slate (plan §6.4).

A mesh edge is a **UV boundary seam** when the UV is discontinuous across it: the
two faces sharing the edge place the edge's endpoints at different UV coordinates
(the unwrap was cut there). This module is **pure** — it operates on a
:class:`~uv_agent.geometry.mesh_graph.MeshGraph` plus a per-loop
:class:`~uv_agent.geometry.solution.UVMap` (both aligned by loop index, exactly as
:func:`uv_agent.blender.uv_extract.extract_mesh_graph_with_uv` returns them), so it
unit-tests without Blender.

Rules (plan §6.4):

- For each mesh edge with two linked faces, compare the UV at both endpoint
  vertices across the two adjacent face loops. If *either* endpoint's UV is
  discontinuous across the edge, the edge is a UV boundary seam.
- Mesh-boundary edges (one linked face) cannot have a UV discontinuity to compare;
  they are reported separately, not auto-added as seams.
- Non-manifold edges (>2 linked faces) are ambiguous; they are reported and
  included as seams only when a clear discontinuity is found among their loops.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

# Loop UVs equal within this tolerance are "the same point" — i.e. continuous
# across the edge (no seam). 1e-6 in normalized UV space is well below a texel.
DEFAULT_UV_WELD_TOL = 1e-6


@dataclass
class UvBoundaryResult:
    """The outcome of :func:`extract_uv_boundary_seams` (plan §6.4 report block).

    ``seam_edges`` are the mesh edge ids to drop into ``user_seam_edges``.
    ``mesh_boundary_edges`` (open edges) and ``non_manifold_edges`` are reported
    separately so the UI can surface them as warnings rather than silently shipping
    them. ``ambiguous_edges`` are edges whose loop/UV data could not be read
    reliably (e.g. an endpoint missing from one face's loops)."""

    seam_edges: list[int] = field(default_factory=list)
    mesh_boundary_edges: list[int] = field(default_factory=list)
    non_manifold_edges: list[int] = field(default_factory=list)
    ambiguous_edges: list[int] = field(default_factory=list)
    # The number of UV islands the layer carries (loop-UV connected components), and how the
    # boundary was derived — surfaced so the run report explains WHY the boundary edge count
    # is what it is (MVP3 §2 Goal A / §0: derived boundary 724 vs an expected larger count).
    island_count: int = 0
    method: str = "uv_loop_discontinuity"

    @property
    def boundary_edge_count(self) -> int:
        return len(self.seam_edges)

    def report(self) -> dict:
        return {
            "boundary_edge_count": self.boundary_edge_count,
            "island_count": self.island_count,
            "mesh_boundary_edge_count": len(self.mesh_boundary_edges),
            "ambiguous_boundary_count": len(self.ambiguous_edges),
            "non_manifold_edge_count": len(self.non_manifold_edges),
            "method": self.method,
            "mesh_boundary_edges": list(self.mesh_boundary_edges),
            "ambiguous_edges": list(self.ambiguous_edges),
            "non_manifold_edges": list(self.non_manifold_edges),
            "uv_layer_missing": False,
        }


def _loop_uv_for_vertex(
    mesh: MeshGraph, uvmap: UVMap, face_id: int, vertex_id: int
) -> tuple[float, float] | None:
    """The UV at ``vertex_id`` as seen by face ``face_id`` (its loop on that vertex).

    Returns ``None`` if the vertex is not a corner of the face (should not happen
    for an edge's own faces, but keeps the caller robust against bad topology)."""
    face = mesh.faces[face_id]
    for loop_index in face.loop_indices:
        if mesh.loops[loop_index].vertex_id == vertex_id:
            return uvmap.get(loop_index)
    return None


def _uv_continuous(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def extract_uv_boundary_seams(
    mesh: MeshGraph, uvmap: UVMap, *, weld_tol: float = DEFAULT_UV_WELD_TOL
) -> UvBoundaryResult:
    """Find the mesh edges that are UV island boundaries (plan §6.4).

    An edge with exactly two faces is a seam when *either* shared endpoint has a
    discontinuous UV across the two faces. Open (mesh-boundary) and non-manifold
    edges are reported separately (see :class:`UvBoundaryResult`).
    """
    result = UvBoundaryResult()

    for edge in mesh.edges:
        v0, v1 = edge.vertex_ids
        faces = edge.face_ids

        if len(faces) == 1:
            result.mesh_boundary_edges.append(edge.id)
            continue
        if len(faces) > 2:
            # Non-manifold: only trust a discontinuity we can actually read off the
            # first two loops; otherwise just report it (plan §6.4 / §14 ambiguity).
            result.non_manifold_edges.append(edge.id)
            fa, fb = faces[0], faces[1]
        elif len(faces) == 2:
            fa, fb = faces
        else:  # 0 faces — a stray edge; nothing to compare.
            result.ambiguous_edges.append(edge.id)
            continue

        a0 = _loop_uv_for_vertex(mesh, uvmap, fa, v0)
        a1 = _loop_uv_for_vertex(mesh, uvmap, fa, v1)
        b0 = _loop_uv_for_vertex(mesh, uvmap, fb, v0)
        b1 = _loop_uv_for_vertex(mesh, uvmap, fb, v1)
        if a0 is None or a1 is None or b0 is None or b1 is None:
            result.ambiguous_edges.append(edge.id)
            continue

        # Discontinuous if EITHER endpoint disagrees across the two faces.
        discontinuous = not (
            _uv_continuous(a0, b0, weld_tol) and _uv_continuous(a1, b1, weld_tol)
        )
        if discontinuous:
            result.seam_edges.append(edge.id)

    result.seam_edges.sort()
    result.mesh_boundary_edges.sort()
    result.non_manifold_edges.sort()
    result.ambiguous_edges.sort()
    # Island count from loop-UV connectivity (the same definition the review report uses).
    # A low boundary edge count is explained by a low island count + welded/continuous UVs,
    # so the report can state why (MVP3 §2 Goal A completion criterion).
    from uv_agent.geometry.evaluation import uv_islands_from_uvmap
    result.island_count = len([isl for isl in uv_islands_from_uvmap(mesh, uvmap) if isl])
    return result
