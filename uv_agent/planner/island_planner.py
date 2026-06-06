"""UV island planning (plan §7.2).

The planner is *deterministic and rule-based*; the LLM does not emit raw island
membership. Instead the agent emits structured actions (``split_island``,
``merge_islands``, ``protect_region`` ...) which mutate an :class:`IslandPlan`.
This keeps the AI in the "intent/strategy" role and the solver in the
"computation" role, exactly as the plan prescribes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from uv_agent.geometry.mesh_graph import Edge, MeshGraph


@dataclass
class PlanConstraints:
    """Quality constraints attached to a plan (plan §7.2 ``constraints``)."""

    preserve_symmetry: bool = False
    max_overlap_ratio: float = 0.0
    padding_px: int = 8
    texture_size_px: int = 1024

    @property
    def padding_uv(self) -> float:
        """Padding converted to UV space (0..1)."""
        return self.padding_px / max(1, self.texture_size_px)

    def to_dict(self) -> dict:
        return {
            "preserve_symmetry": self.preserve_symmetry,
            "max_overlap_ratio": self.max_overlap_ratio,
            "padding_px": self.padding_px,
            "texture_size_px": self.texture_size_px,
        }


@dataclass
class Island:
    island_id: str
    face_ids: list[int]
    priority: str = "normal"  # visible | normal | hidden
    texel_density: str = "normal"  # high | normal | low
    seam_visibility: str = "any"  # avoid_front | any
    projection: str = "planar"  # planar | cylindrical
    protected: bool = False  # excluded from relax/repack (plan §7.6 protect_region)

    def to_dict(self) -> dict:
        return {
            "island_id": self.island_id,
            "face_ids": self.face_ids,
            "priority": self.priority,
            "texel_density": self.texel_density,
            "seam_visibility": self.seam_visibility,
            "projection": self.projection,
            "protected": self.protected,
        }


@dataclass
class IslandPlan:
    islands: list[Island]
    seam_edge_ids: list[int] = field(default_factory=list)
    constraints: PlanConstraints = field(default_factory=PlanConstraints)

    def to_dict(self) -> dict:
        return {
            "islands": [i.to_dict() for i in self.islands],
            "seam_edges": self.seam_edge_ids,
            "constraints": self.constraints.to_dict(),
        }

    def island_by_id(self, island_id: str) -> Island | None:
        for i in self.islands:
            if i.island_id == island_id:
                return i
        return None

    def face_to_island(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for isl in self.islands:
            for fid in isl.face_ids:
                out[fid] = isl.island_id
        return out


def is_seam_edge(
    edge: Edge,
    *,
    angle_threshold: float,
    split_by_material: bool,
    face_material: dict[int, int],
    forced_seam_ids: set[int],
) -> bool:
    """A seam is where islands are allowed/forced to be cut."""
    if edge.id in forced_seam_ids:
        return True
    if edge.is_boundary or edge.is_non_manifold:
        return True
    if edge.is_sharp or edge.is_seam:
        return True
    if edge.dihedral_angle >= angle_threshold:
        return True
    if split_by_material and len(edge.face_ids) == 2:
        if face_material[edge.face_ids[0]] != face_material[edge.face_ids[1]]:
            return True
    return False


def plan_islands(
    mesh: MeshGraph,
    *,
    angle_threshold: float = 30.0,
    split_by_material: bool = True,
    forced_seam_ids: set[int] | None = None,
    constraints: PlanConstraints | None = None,
) -> IslandPlan:
    """Split a mesh into UV islands by flood-filling faces, never crossing a seam.

    This is the Phase 3 "hard-edge / material boundary island split" MVP.
    """
    forced = set(forced_seam_ids or [])
    face_material = {f.id: f.material_index for f in mesh.faces}

    seam_ids = {
        e.id
        for e in mesh.edges
        if is_seam_edge(
            e,
            angle_threshold=angle_threshold,
            split_by_material=split_by_material,
            face_material=face_material,
            forced_seam_ids=forced,
        )
    }

    adjacency = mesh.face_adjacency()
    visited: set[int] = set()
    islands: list[Island] = []

    for face in mesh.faces:
        if face.id in visited:
            continue
        # BFS flood fill, not crossing seam edges.
        component: list[int] = []
        queue = deque([face.id])
        visited.add(face.id)
        while queue:
            cur = queue.popleft()
            component.append(cur)
            for neighbor, edge_id in adjacency[cur]:
                if neighbor in visited or edge_id in seam_ids:
                    continue
                visited.add(neighbor)
                queue.append(neighbor)
        islands.append(Island(island_id=f"island_{len(islands):02d}", face_ids=sorted(component)))

    return IslandPlan(
        islands=islands,
        seam_edge_ids=sorted(seam_ids),
        constraints=constraints or PlanConstraints(),
    )
