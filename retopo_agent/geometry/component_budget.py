"""Component budget policy for decimation (Decimation plan DM3, §6).

At very low target face counts the small detached shells of a multi-component
mesh eat a disproportionate share of the face budget -- the anchor's plateau is
25 shells, 20 of them tiny (plan §1). ZBrush-style decimation does not reduce
every shell by the same ratio: it spreads the budget by *importance* and lets
unimportant debris collapse to a minimal shell, or be removed entirely under an
aggressive target.

This module is the pure-Python planner for that. Given a
:class:`~uv_agent.geometry.mesh_graph.MeshGraph` it:

- measures each connected component (face count, surface area, bbox, materials),
- scores component importance (area / face-count / size, times material weight),
- distributes a target face budget across components by importance under one of
  three policies (``preserve_all`` / ``component_budget`` / ``largest_only``),
- flags tiny components as a minimal-shell or removal candidate (removal is off
  by default; only ``allow_removal`` -- i.e. ``strict_target`` -- permits it), and
- reports the achievable lower bound *with* and *without* tiny-component removal.

It runs on a ``MeshGraph`` so it is unit-tested offline; the Blender adapter is
:mod:`retopo_agent.blender.component_budget`. Executing the per-component collapse
in Blender is the DM5 retry's job -- DM3 produces the plan / report it consumes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from retopo_agent.geometry.diagnosis import (
    DEFAULT_TINY_FACE_FRACTION,
    POLICY_COMPONENT_BUDGET,
    POLICY_LARGEST_ONLY,
    POLICY_PRESERVE_ALL,
)
from uv_agent.geometry.mesh_graph import MeshGraph

# A closed shell needs at least a tetrahedron (4 tris); this is the floor a kept
# component is decimated down to before it stops being a valid surface.
DEFAULT_MIN_SHELL_FACES = 4

# Per-component planned action.
ACTION_DECIMATE = "decimate"  # importance-weighted share of the budget
ACTION_MIN_SHELL = "min_shell"  # kept but collapsed to a minimal shell
ACTION_REMOVE = "remove"  # dropped entirely (only when allow_removal)

# Default importance blend (plan §6 "importance sources"). Surface area dominates
# because face budget should track area; face count and bbox size break ties.
DEFAULT_IMPORTANCE_WEIGHTS = {"area": 0.5, "face": 0.3, "size": 0.2}

_VALID_POLICIES = {POLICY_PRESERVE_ALL, POLICY_COMPONENT_BUDGET, POLICY_LARGEST_ONLY}


def normalize_policy(name: str | None) -> str:
    """Map a CLI ``--component-policy`` value to a canonical policy constant.

    The plan's CLI spells it ``budget`` (plan §6) while the diagnosis recommends
    ``component_budget`` (plan §5); accept both. Unknown / missing -> preserve_all.
    """
    if not name:
        return POLICY_PRESERVE_ALL
    key = str(name).strip().lower()
    if key == "budget":
        return POLICY_COMPONENT_BUDGET
    if key in _VALID_POLICIES:
        return key
    return POLICY_PRESERVE_ALL


@dataclass
class ComponentInfo:
    """Per-connected-component measurements (plan §6 "component별 ... 계산")."""

    id: int  # 0-based, assigned by descending face count (0 = largest)
    face_ids: list[int]
    face_count: int
    vertex_count: int
    surface_area: float
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    bbox_diagonal: float
    bbox_volume: float
    material_indices: list[int]
    is_tiny: bool = False

    @property
    def material_count(self) -> int:
        return len(self.material_indices)


@dataclass
class ComponentPlan:
    """A component plus its planned budget action."""

    component: ComponentInfo
    importance: float
    allocated_budget: int
    action: str

    def to_dict(self) -> dict:
        c = self.component
        return {
            "id": c.id,
            "face_count": c.face_count,
            "vertex_count": c.vertex_count,
            "surface_area": round(c.surface_area, 6),
            "bbox_diagonal": round(c.bbox_diagonal, 6),
            "bbox_volume": round(c.bbox_volume, 6),
            "material_indices": c.material_indices,
            "is_tiny": c.is_tiny,
            "importance": round(self.importance, 6),
            "allocated_budget": self.allocated_budget,
            "action": self.action,
        }


@dataclass
class ComponentBudgetReport:
    policy: str
    target_face_count: int
    allow_removal: bool
    min_shell_faces: int
    total_face_count: int
    component_count: int
    tiny_component_count: int
    components: list[ComponentPlan] = field(default_factory=list)
    allocated_total: int = 0
    removed_component_count: int = 0
    removed_face_count: int = 0
    lower_bound_without_removal: int = 0
    lower_bound_with_removal: int = 0

    @property
    def reachable_without_removal(self) -> bool:
        return self.target_face_count >= self.lower_bound_without_removal

    @property
    def reachable_with_removal(self) -> bool:
        return self.target_face_count >= self.lower_bound_with_removal

    def to_dict(self) -> dict:
        return {
            "policy": self.policy,
            "target_face_count": self.target_face_count,
            "allow_removal": self.allow_removal,
            "min_shell_faces": self.min_shell_faces,
            "total_face_count": self.total_face_count,
            "component_count": self.component_count,
            "tiny_component_count": self.tiny_component_count,
            "allocated_total": self.allocated_total,
            "removed_component_count": self.removed_component_count,
            "removed_face_count": self.removed_face_count,
            "lower_bound_without_removal": self.lower_bound_without_removal,
            "lower_bound_with_removal": self.lower_bound_with_removal,
            "reachable_without_removal": self.reachable_without_removal,
            "reachable_with_removal": self.reachable_with_removal,
            "components": [p.to_dict() for p in self.components],
        }


def analyze_components(
    mesh: MeshGraph, *, tiny_face_fraction: float = DEFAULT_TINY_FACE_FRACTION
) -> list[ComponentInfo]:
    """Measure each vertex-connected component of ``mesh`` (plan §6).

    Components are found with union-find over the vertices of every face (so a
    shell joined only at a vertex still counts as one), then measured for face
    count, surface area, bounding box, and the set of material slots they use.
    Returned sorted by descending face count -- component ``0`` is the largest --
    with ``is_tiny`` set for components below ``tiny_face_fraction`` of total faces.
    """
    if mesh.face_count == 0:
        return []

    parent = list(range(mesh.vertex_count))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for f in mesh.faces:
        vids = f.vertex_ids
        first = vids[0]
        for vid in vids[1:]:
            union(first, vid)

    groups: dict[int, list[int]] = defaultdict(list)
    for f in mesh.faces:
        groups[find(f.vertex_ids[0])].append(f.id)

    coords = np.array([v.co for v in mesh.vertices], dtype=float)
    comps: list[ComponentInfo] = []
    for fids in groups.values():
        vids: set[int] = set()
        area = 0.0
        mats: set[int] = set()
        for fid in fids:
            f = mesh.faces[fid]
            area += f.area_3d
            mats.add(f.material_index)
            vids.update(f.vertex_ids)
        pts = coords[sorted(vids)]
        bbmin = pts.min(axis=0)
        bbmax = pts.max(axis=0)
        extent = bbmax - bbmin
        comps.append(
            ComponentInfo(
                id=0,
                face_ids=sorted(fids),
                face_count=len(fids),
                vertex_count=len(vids),
                surface_area=float(area),
                bbox_min=(float(bbmin[0]), float(bbmin[1]), float(bbmin[2])),
                bbox_max=(float(bbmax[0]), float(bbmax[1]), float(bbmax[2])),
                bbox_diagonal=float(np.linalg.norm(extent)),
                bbox_volume=float(extent[0] * extent[1] * extent[2]),
                material_indices=sorted(mats),
            )
        )

    comps.sort(key=lambda c: (c.face_count, c.surface_area), reverse=True)
    tiny_threshold = max(1.0, tiny_face_fraction * mesh.face_count)
    for i, c in enumerate(comps):
        c.id = i
        c.is_tiny = c.face_count < tiny_threshold
    return comps


def _importance(
    c: ComponentInfo,
    totals: dict,
    weights: dict,
    material_importance: dict[int, float] | None,
) -> float:
    area = c.surface_area / totals["area"] if totals["area"] else 0.0
    face = c.face_count / totals["faces"] if totals["faces"] else 0.0
    size = c.bbox_diagonal / totals["bbox"] if totals["bbox"] else 0.0
    base = weights["area"] * area + weights["face"] * face + weights["size"] * size
    mat_weight = 1.0
    if material_importance:
        mat_weight = max((material_importance.get(m, 1.0) for m in c.material_indices), default=1.0)
    return base * mat_weight


def plan_component_budget(
    mesh: MeshGraph,
    target_face_count: int,
    *,
    policy: str = POLICY_PRESERVE_ALL,
    allow_removal: bool = False,
    min_shell_faces: int = DEFAULT_MIN_SHELL_FACES,
    tiny_face_fraction: float = DEFAULT_TINY_FACE_FRACTION,
    importance_weights: dict | None = None,
    material_importance: dict[int, float] | None = None,
) -> ComponentBudgetReport:
    """Distribute ``target_face_count`` across ``mesh``'s components (plan §6).

    ``policy`` selects how non-dominant shells are treated:

    - ``preserve_all`` -- every shell kept and given an importance-weighted share.
    - ``component_budget`` -- tiny shells collapse to a minimal shell (or are
      removed when ``allow_removal``); the rest share the budget by importance.
    - ``largest_only`` -- only the largest shell is decimated to target; the rest
      collapse to a minimal shell (or are removed when ``allow_removal``).

    Each active component's budget is clamped to ``[min(face_count, min_shell), face_count]``
    (decimation can only reduce). At least one component is always kept active so a
    policy never empties the mesh. The report also gives the theoretical lower-bound
    face count with and without tiny-component removal, for the §6 comparison.
    """
    policy = normalize_policy(policy)
    weights = importance_weights or DEFAULT_IMPORTANCE_WEIGHTS

    comps = analyze_components(mesh, tiny_face_fraction=tiny_face_fraction)
    total_faces = mesh.face_count
    tiny_count = sum(1 for c in comps if c.is_tiny)

    if not comps:
        return ComponentBudgetReport(
            policy=policy,
            target_face_count=target_face_count,
            allow_removal=allow_removal,
            min_shell_faces=min_shell_faces,
            total_face_count=total_faces,
            component_count=0,
            tiny_component_count=0,
        )

    largest = comps[0]  # sorted descending by face count

    # Classify each component's role under the policy.
    roles: dict[int, str] = {}
    for c in comps:
        if policy == POLICY_PRESERVE_ALL:
            roles[c.id] = ACTION_DECIMATE
        elif policy == POLICY_LARGEST_ONLY:
            roles[c.id] = ACTION_DECIMATE if c.id == largest.id else (
                ACTION_REMOVE if allow_removal else ACTION_MIN_SHELL
            )
        else:  # component_budget
            if c.is_tiny and c.id != largest.id:
                roles[c.id] = ACTION_REMOVE if allow_removal else ACTION_MIN_SHELL
            else:
                roles[c.id] = ACTION_DECIMATE
    if not any(r == ACTION_DECIMATE for r in roles.values()):
        roles[largest.id] = ACTION_DECIMATE  # never empty the mesh

    # Reserve faces for kept-as-shell components; removed contribute nothing.
    shell_faces = sum(
        min(c.face_count, min_shell_faces) for c in comps if roles[c.id] == ACTION_MIN_SHELL
    )
    removed = [c for c in comps if roles[c.id] == ACTION_REMOVE]
    active = [c for c in comps if roles[c.id] == ACTION_DECIMATE]

    totals = {
        "area": sum(c.surface_area for c in active),
        "faces": sum(c.face_count for c in active),
        "bbox": sum(c.bbox_diagonal for c in active),
    }
    importances = {c.id: _importance(c, totals, weights, material_importance) for c in active}
    importance_total = sum(importances.values())
    active_target = max(0, target_face_count - shell_faces)

    plans: list[ComponentPlan] = []
    allocated_total = 0
    for c in comps:
        role = roles[c.id]
        if role == ACTION_REMOVE:
            alloc = 0
        elif role == ACTION_MIN_SHELL:
            alloc = min(c.face_count, min_shell_faces)
        else:  # decimate -- importance-weighted share, clamped to a valid range
            if importance_total > 0:
                share = importances[c.id] / importance_total
            else:
                share = 1.0 / len(active)
            alloc = round(active_target * share)
            lo = min(c.face_count, min_shell_faces)
            alloc = max(lo, min(alloc, c.face_count))
        allocated_total += alloc
        plans.append(ComponentPlan(c, importances.get(c.id, 0.0), alloc, role))

    plans.sort(key=lambda p: p.component.id)

    # Theoretical lower bounds (independent of the policy): the minimum total face
    # count keeping every shell at a minimal shell, vs dropping the tiny ones.
    lb_without = sum(min(c.face_count, min_shell_faces) for c in comps)
    kept = [c for c in comps if not c.is_tiny] or [largest]
    lb_with = sum(min(c.face_count, min_shell_faces) for c in kept)

    return ComponentBudgetReport(
        policy=policy,
        target_face_count=target_face_count,
        allow_removal=allow_removal,
        min_shell_faces=min_shell_faces,
        total_face_count=total_faces,
        component_count=len(comps),
        tiny_component_count=tiny_count,
        components=plans,
        allocated_total=allocated_total,
        removed_component_count=len(removed),
        removed_face_count=sum(c.face_count for c in removed),
        lower_bound_without_removal=lb_without,
        lower_bound_with_removal=lb_with,
    )
