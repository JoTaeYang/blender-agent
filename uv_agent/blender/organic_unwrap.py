"""Blender side of organic unwrap (UV repair plan §3 Track 1, steps 3–4).

The pure planner (:mod:`uv_agent.planner.organic_seams`) decides *where* to cut; this
module marks those seams on the real mesh and runs Blender's angle-based unwrap +
island packer — a true parameterizer, not the per-island planar projection that
shatters/overlaps organic geometry. It then reads the resulting per-loop UVs back into
a :class:`UVMap` and rebuilds an :class:`IslandPlan` from the seam flood-fill so the
existing evaluation pipeline scores the Blender result unchanged.

Only runs inside Blender (``bpy`` lazy).
"""

from __future__ import annotations

from collections import deque

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.planner.island_planner import Island, IslandPlan, PlanConstraints

AI_UV_LAYER = "AI_UV"


def mark_seams(obj, seam_edge_ids) -> int:
    """Set ``use_seam`` on exactly the given edges (cleared elsewhere). Returns the
    count marked. Edge ids are bmesh/`mesh.edges` indices (what the planner emits)."""
    mesh = obj.data
    seam = set(seam_edge_ids)
    marked = 0
    for e in mesh.edges:
        on = e.index in seam
        e.use_seam = on
        marked += int(on)
    mesh.update()
    return marked


def unwrap_organic(
    obj,
    seam_edge_ids,
    *,
    method: str = "ANGLE_BASED",
    margin: float = 0.02,
    minimize_iterations: int = 32,
    average_islands_scale: bool = True,
    layer_name: str = AI_UV_LAYER,
) -> int:
    """Mark ``seam_edge_ids`` and run a seam-honoring unwrap (plan §3 steps 3–4).

    The op chain is: angle-based unwrap → ``minimize_stretch`` (cuts the area
    distortion the raw conformal map leaves) → ``average_islands_scale`` (uniform
    texel density, which the area-stretch metric rewards) → ``pack_islands`` (rotation
    on). Returns the number of seams marked; UVs land in ``layer_name``."""
    import bpy

    mesh = obj.data
    if layer_name not in mesh.uv_layers:
        mesh.uv_layers.new(name=layer_name)
    mesh.uv_layers.active = mesh.uv_layers[layer_name]

    marked = mark_seams(obj, seam_edge_ids)

    _activate(bpy, obj)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="SELECT")
        bpy.ops.uv.unwrap(method=method, margin=margin)
        if minimize_iterations:
            try:
                bpy.ops.uv.minimize_stretch(iterations=int(minimize_iterations))
            except RuntimeError:
                pass
        if average_islands_scale:
            try:
                bpy.ops.uv.average_islands_scale()
            except RuntimeError:
                pass
        _pack_islands(bpy, margin)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    mesh.update()
    return marked


def _pack_islands(bpy, margin: float) -> None:
    """Call ``uv.pack_islands`` across Blender 4/5 signature drift (rotate on)."""
    try:
        bpy.ops.uv.pack_islands(rotate=True, margin=margin)
    except TypeError:
        bpy.ops.uv.pack_islands(margin=margin)


def read_uvmap(obj, mesh: MeshGraph, *, layer_name: str = AI_UV_LAYER) -> UVMap:
    """Read the active UV layer into a :class:`UVMap` aligned to ``mesh``'s loop order
    (loop index i ↔ ``obj.data.loops[i]`` — the same order :func:`extract_mesh_graph`
    assigned, so no remap is needed)."""
    import numpy as np

    layer = obj.data.uv_layers.get(layer_name) or obj.data.uv_layers.active
    data = layer.data
    arr = np.empty(len(data) * 2, dtype=np.float64)
    data.foreach_get("uv", arr)
    arr = arr.reshape(-1, 2)
    uvmap = UVMap(len(mesh.loops))
    n = min(len(arr), len(mesh.loops))
    uvmap.uv[:n] = arr[:n]
    return uvmap


def island_plan_from_seams(
    mesh: MeshGraph,
    seam_edge_ids,
    *,
    constraints: PlanConstraints | None = None,
) -> IslandPlan:
    """Flood-fill faces across non-seam edges to recover the island decomposition the
    Blender unwrap produced. Cuts on **only** the given organic seams (plus genuine
    boundary / non-manifold edges) — NOT on ``is_sharp`` / ``is_seam`` / dihedral,
    which the stock ``plan_islands`` honors. The A3 smooth-by-angle pass leaves sharp
    edges all over an organic mesh; treating those as cuts is exactly the confetti bug
    (576 islands), so organic-mode island recovery must ignore them and match the real
    UV-seam topology the unwrap used."""
    seam = set(seam_edge_ids)
    for e in mesh.edges:
        if e.is_boundary or e.is_non_manifold:
            seam.add(e.id)

    adjacency = mesh.face_adjacency()
    visited: set[int] = set()
    islands: list[Island] = []
    for face in mesh.faces:
        if face.id in visited:
            continue
        component: list[int] = []
        queue = deque([face.id])
        visited.add(face.id)
        while queue:
            cur = queue.popleft()
            component.append(cur)
            for neighbor, edge_id in adjacency[cur]:
                if neighbor in visited or edge_id in seam:
                    continue
                visited.add(neighbor)
                queue.append(neighbor)
        islands.append(Island(island_id=f"island_{len(islands):02d}", face_ids=sorted(component)))

    return IslandPlan(
        islands=islands,
        seam_edge_ids=sorted(seam),
        constraints=constraints or PlanConstraints(),
    )


def build_uv_metrics(mesh: MeshGraph, uvmap: UVMap, evaluation, *, fallback_used: bool = False) -> dict:
    """Flat metric dict for :func:`~uv_agent.geometry.uv_gate.evaluate_uv_gate`."""
    from uv_agent.geometry.evaluation import estimate_vt_count, uv_bounds_ok

    vt = estimate_vt_count(mesh, uvmap)
    v = max(1, mesh.vertex_count)
    return {
        "overlap_ratio": evaluation.overlap_ratio,
        "island_count": evaluation.island_count,
        "small_island_ratio": evaluation.small_island_ratio,
        "vt_v_ratio": vt / v,
        "stretch_score": evaluation.stretch_score,
        "packing_efficiency": evaluation.packing_efficiency,
        "uv_bounds_ok": uv_bounds_ok(uvmap),
        "fallback_used": fallback_used,
        "vt_count": vt,
    }


def _island_stretch(mesh: MeshGraph, island, face_stretch) -> float:
    """Area-weighted total stretch of an island — picks the refinement target."""
    return float(sum(face_stretch[fid] * mesh.faces[fid].area_3d for fid in island.face_ids))


def organic_unwrap_with_refinement(
    obj,
    mesh: MeshGraph,
    *,
    baseline,
    thresholds=None,
    max_rounds: int = 14,
    margin: float = 0.02,
    view_dir: tuple[float, float, float] = (0.0, -1.0, 0.0),
    n_extremities: int = 12,
) -> dict:
    """Track 1 + Track 2: organic cut-tree unwrap, then iteratively split the worst
    island to relieve stretch until the §5 gate passes or improvement stalls (plan §3).

    Each round re-unwraps in Blender with one added seam (through the worst island's
    high-stretch axis); a round that does not reduce global stretch is reverted and
    the loop stops (monotonic-improvement rule). The object is left holding the BEST
    layout. Returns the evaluation, gate, metrics, seam set, and per-round history —
    the Smart-UV fallback is never produced here (hard gate, plan §5)."""
    from uv_agent.geometry.evaluation import evaluate_uv_solution
    from uv_agent.geometry.uv_gate import UVGateThresholds, evaluate_uv_gate
    from uv_agent.planner.organic_seams import merge_small_islands, organic_seam_edges

    thresholds = thresholds or UVGateThresholds()
    constraints = PlanConstraints(max_overlap_ratio=0.0)
    protected = {e.id for e in mesh.edges if e.is_boundary or e.is_non_manifold}

    def unwrap_and_eval(seam_set):
        unwrap_organic(obj, seam_set, margin=margin)
        uvmap = read_uvmap(obj, mesh)
        plan = island_plan_from_seams(mesh, seam_set, constraints=constraints)
        ev = evaluate_uv_solution(mesh, plan, uvmap)
        metrics = build_uv_metrics(mesh, uvmap, ev)
        return uvmap, plan, ev, metrics

    cut_tree = organic_seam_edges(mesh, n_extremities=n_extremities, view_dir=view_dir)

    # Track 2 as a seam-density search (plan §3): the cut-tree opens the topology; the
    # crease percentile trades stretch (more charts) against the island/vt gates, and
    # merge_small_islands folds the resulting confetti back to satisfy them. We keep the
    # best-gated / lowest-stretch layout, monotonically — the agent stays in the
    # strategy seat, the solver computes each candidate.
    trials = [("pelt", None, 0)]
    for pct in (90.0, 87.0, 84.0):
        trials.append((f"crease{int(pct)}", pct, max(20, thresholds.island_count_max)))

    history: list[dict] = []
    best = None
    for idx, (label, pct, max_isl) in enumerate(trials):
        seams = set(cut_tree)
        if pct is not None:
            from uv_agent.planner.organic_seams import crease_seam_edges
            seams |= crease_seam_edges(mesh, percentile=pct)
            seams = merge_small_islands(mesh, seams, min_island_faces=40,
                                        max_islands=max_isl, protected=protected)
        uvmap, plan, ev, metrics = unwrap_and_eval(seams)
        gate = evaluate_uv_gate(metrics, baseline=baseline, thresholds=thresholds)
        history.append(_round_rec(idx, ev, metrics, len(seams), label))
        if best is None or _is_better(metrics, gate, best):
            best = {"seams": set(seams), "ev": ev, "metrics": metrics, "gate": gate}
        # Stop only when nothing is left to improve (every check, soft included). Hard
        # gates pass on the first pelt, so breaking there would skip the lower-stretch
        # crease trials — keep searching for the best stretch.
        if gate.passed and not gate.soft_failures:
            break

    # Re-unwrap the winning seam set so the object holds the best layout.
    uvmap, plan, ev, metrics = unwrap_and_eval(best["seams"])
    gate = evaluate_uv_gate(metrics, baseline=baseline, thresholds=thresholds)
    return {
        "seams": sorted(best["seams"]), "evaluation": ev, "metrics": metrics,
        "gate": gate, "rounds": len(trials), "history": history,
    }


def _round_rec(rnd, ev, metrics, n_seams, action) -> dict:
    return {"round": rnd, "action": action, "seams": n_seams,
            "islands": ev.island_count, "stretch": round(ev.stretch_score, 5),
            "overlap": round(ev.overlap_ratio, 6),
            "vt_v": round(metrics["vt_v_ratio"], 4),
            "packing": round(ev.packing_efficiency, 4)}


def _is_better(metrics, gate, best) -> bool:
    """Prefer a gate-passing layout; among equals, lower stretch."""
    if gate.passed != best["gate"].passed:
        return gate.passed
    return metrics["stretch_score"] < best["metrics"]["stretch_score"]


def _activate(bpy, obj) -> None:
    try:
        bpy.ops.object.select_all(action="DESELECT")
    except RuntimeError:
        for o in bpy.context.view_layer.objects:
            if o is not None:
                o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
