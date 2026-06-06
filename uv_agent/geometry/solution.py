"""UV solution representation (plan §7.3 output format).

The canonical working representation is :class:`UVMap`: a dense ``(n_loops, 2)``
array indexed by loop index, because Blender stores UVs per loop
(``mesh.uv_layers.active.data[loop_index].uv``). :class:`UVSolution` is the
serializable export form returned to the agent / web app / Blender writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph


@dataclass
class IslandTransform:
    island_id: str
    rotation_deg: float = 0.0
    scale: float = 1.0
    translation: tuple[float, float] = (0.0, 0.0)

    def to_dict(self) -> dict:
        return {
            "island_id": self.island_id,
            "rotation_deg": self.rotation_deg,
            "scale": self.scale,
            "translation": list(self.translation),
        }


class UVMap:
    """Dense per-loop UV storage. ``uv[loop_index] -> (u, v)``."""

    def __init__(self, n_loops: int):
        self.uv = np.zeros((n_loops, 2), dtype=float)

    @classmethod
    def for_mesh(cls, mesh: MeshGraph) -> "UVMap":
        return cls(len(mesh.loops))

    def __len__(self) -> int:
        return len(self.uv)

    def get(self, loop_index: int) -> tuple[float, float]:
        u, v = self.uv[loop_index]
        return float(u), float(v)

    def set(self, loop_index: int, u: float, v: float) -> None:
        self.uv[loop_index] = (u, v)

    def face_uvs(self, mesh: MeshGraph, face_id: int) -> np.ndarray:
        return self.uv[mesh.faces[face_id].loop_indices]

    def copy(self) -> "UVMap":
        out = UVMap(len(self.uv))
        out.uv = self.uv.copy()
        return out


@dataclass
class UVSolution:
    """Serializable UV result (plan §7.3)."""

    object_id: str
    uv_coordinates: list[dict] = field(default_factory=list)
    island_transforms: list[IslandTransform] = field(default_factory=list)

    @classmethod
    def from_uvmap(
        cls,
        mesh: MeshGraph,
        uvmap: UVMap,
        transforms: list[IslandTransform] | None = None,
    ) -> "UVSolution":
        coords = []
        for loop in mesh.loops:
            u, v = uvmap.get(loop.index)
            coords.append(
                {"face_id": loop.face_id, "loop_index": loop.index, "uv": [u, v]}
            )
        return cls(
            object_id=mesh.object_id,
            uv_coordinates=coords,
            island_transforms=list(transforms or []),
        )

    def to_uvmap(self, mesh: MeshGraph) -> UVMap:
        uvmap = UVMap.for_mesh(mesh)
        for entry in self.uv_coordinates:
            u, v = entry["uv"]
            uvmap.set(entry["loop_index"], float(u), float(v))
        return uvmap

    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "uv_coordinates": self.uv_coordinates,
            "island_transforms": [t.to_dict() for t in self.island_transforms],
        }
