"""Plan-mutating operations driven by AI actions (plan §7.6).

These take an :class:`IslandPlan` and return a new, mutated plan. They are the
``split_island`` / ``merge_islands`` / ``protect_region`` handlers; the
transform-style actions (rotate/scale/translate/repack/relax) act on the solver
stage instead and are handled in the pipeline.
"""

from __future__ import annotations

import copy

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.planner.island_planner import Island, IslandPlan


def _renumber(islands: list[Island]) -> None:
    # Keep stable, human-readable ids while preserving any semantic ids that
    # don't look auto-generated.
    for idx, isl in enumerate(islands):
        if isl.island_id.startswith("island_"):
            isl.island_id = f"island_{idx:02d}"


def split_island(plan: IslandPlan, island_id: str, target_faces: list[int]) -> IslandPlan:
    """Pull ``target_faces`` out of their island into a new island.

    Faces not present in the target island are ignored. The boundary between
    the two groups becomes an implicit seam.
    """
    plan = copy.deepcopy(plan)
    src = plan.island_by_id(island_id)
    if src is None:
        return plan
    targets = [f for f in target_faces if f in src.face_ids]
    if not targets or len(targets) == len(src.face_ids):
        return plan  # nothing to split, or would empty the source
    remaining = [f for f in src.face_ids if f not in targets]
    src.face_ids = sorted(remaining)
    new_id = f"{island_id}_split"
    # Avoid id collisions.
    n = 1
    while plan.island_by_id(new_id) is not None:
        new_id = f"{island_id}_split{n}"
        n += 1
    plan.islands.append(
        Island(
            island_id=new_id,
            face_ids=sorted(targets),
            priority=src.priority,
            texel_density=src.texel_density,
            seam_visibility=src.seam_visibility,
            projection=src.projection,
        )
    )
    return plan


def merge_islands(plan: IslandPlan, island_ids: list[int] | list[str]) -> IslandPlan:
    """Merge several islands into the first one."""
    plan = copy.deepcopy(plan)
    ids = [str(i) for i in island_ids]
    keep = plan.island_by_id(ids[0]) if ids else None
    if keep is None:
        return plan
    for other_id in ids[1:]:
        other = plan.island_by_id(other_id)
        if other is None or other is keep:
            continue
        keep.face_ids = sorted(set(keep.face_ids) | set(other.face_ids))
        plan.islands.remove(other)
    return plan


def merge_small_islands(plan: IslandPlan, min_faces: int = 1) -> IslandPlan:
    """Merge islands smaller than ``min_faces`` faces into the largest island
    (a coarse ``merge_islands`` heuristic used by the repair planner)."""
    plan = copy.deepcopy(plan)
    if len(plan.islands) <= 1:
        return plan
    largest = max(plan.islands, key=lambda i: len(i.face_ids))
    small = [i for i in plan.islands if i is not largest and len(i.face_ids) <= min_faces]
    for s in small:
        largest.face_ids = sorted(set(largest.face_ids) | set(s.face_ids))
        plan.islands.remove(s)
    return plan


def protect_region(plan: IslandPlan, face_ids: list[int]) -> IslandPlan:
    """Mark every island that contains any of ``face_ids`` as protected so the
    repair loop won't re-relax or re-pack it (plan §7.6 ``protect_region``)."""
    plan = copy.deepcopy(plan)
    targets = set(face_ids)
    for isl in plan.islands:
        if targets.intersection(isl.face_ids):
            isl.protected = True
    return plan


def faces_for_region(mesh: MeshGraph, region: str) -> list[int]:
    """Resolve a coarse named region (used when the agent references a region by
    name rather than explicit face ids). MVP heuristic by position keywords.
    """
    region = (region or "").lower()
    centers = {
        f.id: tuple(sum(mesh.vertices[v].co[k] for v in f.vertex_ids) / len(f.vertex_ids) for k in range(3))
        for f in mesh.faces
    }
    if not centers:
        return []
    xs = [c[0] for c in centers.values()]
    ys = [c[1] for c in centers.values()]
    zs = [c[2] for c in centers.values()]

    def pick(axis: int, top: bool) -> list[int]:
        vals = {"x": xs, "y": ys, "z": zs}[["x", "y", "z"][axis]]
        mid = (min(vals) + max(vals)) / 2
        return sorted(fid for fid, c in centers.items() if (c[axis] >= mid) == top)

    if "front" in region:
        return pick(1, top=False)
    if "back" in region:
        return pick(1, top=True)
    if "top" in region or "upper" in region:
        return pick(2, top=True)
    if "bottom" in region or "lower" in region:
        return pick(2, top=False)
    if "left" in region:
        return pick(0, top=False)
    if "right" in region:
        return pick(0, top=True)
    return []
