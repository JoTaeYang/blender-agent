"""Structured mesh representation shared by the AI agent and the geometry solver.

This mirrors plan §7.1 ``extract_mesh_graph``: vertices, edges, faces and loops
with adjacency, dihedral angles and boundary / non-manifold detection.

The :meth:`MeshGraph.from_faces` builder lets us construct a graph from raw
polygon data (no Blender required), which is what the synthetic fixtures and the
unit tests use. The Blender adapter (:mod:`uv_agent.blender.extract`) produces an
equivalent graph from a ``bmesh``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

Vec3 = tuple[float, float, float]


@dataclass
class Vertex:
    id: int
    co: Vec3  # 3D position


@dataclass
class Loop:
    """A per-face corner. UV coordinates live per-loop in Blender, so this is the
    atomic unit the solver writes to."""

    index: int
    vertex_id: int
    face_id: int


@dataclass
class Face:
    id: int
    vertex_ids: list[int]
    loop_indices: list[int]
    edge_ids: list[int]
    normal: Vec3
    area_3d: float
    material_index: int = 0


@dataclass
class Edge:
    id: int
    vertex_ids: tuple[int, int]
    face_ids: list[int]
    dihedral_angle: float  # degrees, angle between adjacent face normals (0 = flat)
    is_boundary: bool
    is_non_manifold: bool
    is_sharp: bool = False
    is_seam: bool = False


def _newell_normal_and_area(coords: np.ndarray) -> tuple[np.ndarray, float]:
    """Newell's method: robust polygon normal + planar area for any (planar) n-gon."""
    n = np.zeros(3)
    k = len(coords)
    for i in range(k):
        cur = coords[i]
        nxt = coords[(i + 1) % k]
        n[0] += (cur[1] - nxt[1]) * (cur[2] + nxt[2])
        n[1] += (cur[2] - nxt[2]) * (cur[0] + nxt[0])
        n[2] += (cur[0] - nxt[0]) * (cur[1] + nxt[1])
    length = float(np.linalg.norm(n))
    if length < 1e-12:
        return np.array([0.0, 0.0, 1.0]), 0.0
    return n / length, length / 2.0


def _angle_between(n1: np.ndarray, n2: np.ndarray) -> float:
    d = float(np.clip(np.dot(n1, n2), -1.0, 1.0))
    return math.degrees(math.acos(d))


@dataclass
class MeshGraph:
    object_id: str
    vertices: list[Vertex]
    edges: list[Edge]
    faces: list[Face]
    loops: list[Loop]
    _edge_index: dict[tuple[int, int], int] = field(default_factory=dict, repr=False)

    # -- lookups -----------------------------------------------------------
    @property
    def vertex_count(self) -> int:
        return len(self.vertices)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    @property
    def face_count(self) -> int:
        return len(self.faces)

    def vertex_co(self, vertex_id: int) -> np.ndarray:
        return np.asarray(self.vertices[vertex_id].co, dtype=float)

    def face(self, face_id: int) -> Face:
        return self.faces[face_id]

    def edge(self, edge_id: int) -> Edge:
        return self.edges[edge_id]

    def loop(self, loop_index: int) -> Loop:
        return self.loops[loop_index]

    def edge_key(self, a: int, b: int) -> int:
        return self._edge_index[(a, b) if a < b else (b, a)]

    def face_adjacency(self) -> dict[int, list[tuple[int, int]]]:
        """face_id -> list of (neighbor_face_id, shared_edge_id)."""
        adj: dict[int, list[tuple[int, int]]] = {f.id: [] for f in self.faces}
        for e in self.edges:
            if len(e.face_ids) == 2:
                a, b = e.face_ids
                adj[a].append((b, e.id))
                adj[b].append((a, e.id))
        return adj

    # -- construction ------------------------------------------------------
    @classmethod
    def from_faces(
        cls,
        object_id: str,
        vertices: Sequence[Vec3],
        faces: Sequence[Sequence[int]],
        *,
        material_indices: Sequence[int] | None = None,
        sharp_edge_keys: Iterable[tuple[int, int]] | None = None,
        seam_edge_keys: Iterable[tuple[int, int]] | None = None,
    ) -> "MeshGraph":
        verts = [Vertex(i, (float(x), float(y), float(z))) for i, (x, y, z) in enumerate(vertices)]
        coords_all = np.asarray(vertices, dtype=float)

        sharp = {_norm_key(*k) for k in (sharp_edge_keys or [])}
        seams = {_norm_key(*k) for k in (seam_edge_keys or [])}

        # First pass: build faces, loops, and discover edges.
        loops: list[Loop] = []
        face_objs: list[Face] = []
        edge_keys: dict[tuple[int, int], int] = {}
        edge_face_ids: list[list[int]] = []
        edge_key_list: list[tuple[int, int]] = []

        def get_edge(a: int, b: int) -> int:
            key = _norm_key(a, b)
            idx = edge_keys.get(key)
            if idx is None:
                idx = len(edge_key_list)
                edge_keys[key] = idx
                edge_key_list.append(key)
                edge_face_ids.append([])
            return idx

        for fid, vids in enumerate(faces):
            vids = list(vids)
            loop_indices: list[int] = []
            for vid in vids:
                loops.append(Loop(index=len(loops), vertex_id=vid, face_id=fid))
                loop_indices.append(len(loops) - 1)
            edge_ids: list[int] = []
            for i in range(len(vids)):
                eidx = get_edge(vids[i], vids[(i + 1) % len(vids)])
                edge_ids.append(eidx)
                edge_face_ids[eidx].append(fid)
            normal, area = _newell_normal_and_area(coords_all[vids])
            mat = int(material_indices[fid]) if material_indices is not None else 0
            face_objs.append(
                Face(
                    id=fid,
                    vertex_ids=vids,
                    loop_indices=loop_indices,
                    edge_ids=edge_ids,
                    normal=(float(normal[0]), float(normal[1]), float(normal[2])),
                    area_3d=float(area),
                    material_index=mat,
                )
            )

        # Second pass: finalize edges with dihedral / boundary / manifold flags.
        edge_objs: list[Edge] = []
        for eidx, key in enumerate(edge_key_list):
            fids = edge_face_ids[eidx]
            is_boundary = len(fids) == 1
            is_non_manifold = len(fids) > 2
            if len(fids) == 2:
                n1 = np.asarray(face_objs[fids[0]].normal)
                n2 = np.asarray(face_objs[fids[1]].normal)
                dihedral = _angle_between(n1, n2)
            else:
                dihedral = 0.0
            edge_objs.append(
                Edge(
                    id=eidx,
                    vertex_ids=key,
                    face_ids=list(fids),
                    dihedral_angle=float(dihedral),
                    is_boundary=is_boundary,
                    is_non_manifold=is_non_manifold,
                    is_sharp=key in sharp,
                    is_seam=key in seams,
                )
            )

        return cls(
            object_id=object_id,
            vertices=verts,
            edges=edge_objs,
            faces=face_objs,
            loops=loops,
            _edge_index=dict(edge_keys),
        )

    # -- serialization (plan §7.1 export format) ---------------------------
    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "vertex_count": self.vertex_count,
            "edge_count": self.edge_count,
            "face_count": self.face_count,
            "vertices": [{"vertex_id": v.id, "co": list(v.co)} for v in self.vertices],
            "faces": [
                {
                    "face_id": f.id,
                    "vertex_ids": f.vertex_ids,
                    "loop_indices": f.loop_indices,
                    "edge_ids": f.edge_ids,
                    "normal": list(f.normal),
                    "area_3d": f.area_3d,
                    "material_index": f.material_index,
                }
                for f in self.faces
            ],
            "edges": [
                {
                    "edge_id": e.id,
                    "vertex_ids": list(e.vertex_ids),
                    "face_ids": e.face_ids,
                    "dihedral_angle": e.dihedral_angle,
                    "is_boundary": e.is_boundary,
                    "is_non_manifold": e.is_non_manifold,
                    "is_sharp": e.is_sharp,
                    "is_seam": e.is_seam,
                }
                for e in self.edges
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MeshGraph":
        vertices = [tuple(v["co"]) for v in data["vertices"]]
        faces = [f["vertex_ids"] for f in data["faces"]]
        mats = [f.get("material_index", 0) for f in data["faces"]]
        sharp = [tuple(e["vertex_ids"]) for e in data["edges"] if e.get("is_sharp")]
        seams = [tuple(e["vertex_ids"]) for e in data["edges"] if e.get("is_seam")]
        return cls.from_faces(
            data["object_id"],
            vertices,
            faces,
            material_indices=mats,
            sharp_edge_keys=sharp,
            seam_edge_keys=seams,
        )


def _norm_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)
