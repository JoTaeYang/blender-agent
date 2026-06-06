"""Coordinate relaxation for an island (plan §7.3 "coordinate relaxation").

MVP relaxation = boundary-preserving Laplacian smoothing of interior UV
vertices. It reduces local distortion/noise in the projected layout while
keeping the island outline (and therefore packing footprint) stable.

Within an island, loops that share a mesh vertex share one UV coordinate
(projection guarantees this), so we smooth per UV-vertex and write the result
back to every loop of that vertex.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


@dataclass
class IslandTopology:
    vertex_ids: list[int]
    neighbors: dict[int, set[int]]
    boundary: set[int]
    loops_of_vertex: dict[int, list[int]] = field(default_factory=dict)

    @property
    def interior(self) -> list[int]:
        return [v for v in self.vertex_ids if v not in self.boundary]


def build_island_topology(mesh: MeshGraph, face_ids: list[int]) -> IslandTopology:
    face_set = set(face_ids)
    edge_use: dict[tuple[int, int], int] = {}
    neighbors: dict[int, set[int]] = {}
    loops_of_vertex: dict[int, list[int]] = {}

    for fid in face_ids:
        f = mesh.faces[fid]
        vids = f.vertex_ids
        for loop_index in f.loop_indices:
            vid = mesh.loops[loop_index].vertex_id
            loops_of_vertex.setdefault(vid, []).append(loop_index)
        for i in range(len(vids)):
            a, b = vids[i], vids[(i + 1) % len(vids)]
            key = (a, b) if a < b else (b, a)
            edge_use[key] = edge_use.get(key, 0) + 1
            neighbors.setdefault(a, set()).add(b)
            neighbors.setdefault(b, set()).add(a)

    boundary: set[int] = set()
    for (a, b), count in edge_use.items():
        # An edge used by only one island face is on the island's border.
        if count == 1:
            boundary.add(a)
            boundary.add(b)

    return IslandTopology(
        vertex_ids=list(loops_of_vertex.keys()),
        neighbors=neighbors,
        boundary=boundary,
        loops_of_vertex=loops_of_vertex,
    )


def relax_island(
    mesh: MeshGraph,
    face_ids: list[int],
    uvmap: UVMap,
    *,
    iterations: int = 10,
    lam: float = 0.5,
) -> None:
    """In-place Laplacian smoothing. Boundary UV vertices stay fixed."""
    topo = build_island_topology(mesh, face_ids)
    if not topo.interior:
        return

    # Current UV per vertex (read one representative loop per vertex).
    pos = {v: np.asarray(uvmap.get(topo.loops_of_vertex[v][0])) for v in topo.vertex_ids}

    for _ in range(iterations):
        updates = {}
        for v in topo.interior:
            nbrs = [n for n in topo.neighbors.get(v, ()) if n in pos]
            if not nbrs:
                continue
            centroid = np.mean([pos[n] for n in nbrs], axis=0)
            updates[v] = pos[v] + lam * (centroid - pos[v])
        pos.update(updates)

    # Write back to every loop of each vertex.
    for v in topo.interior:
        u, w = float(pos[v][0]), float(pos[v][1])
        for loop_index in topo.loops_of_vertex[v]:
            uvmap.set(loop_index, u, w)
