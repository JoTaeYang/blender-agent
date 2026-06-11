"""Phase P2 — QuadriFlow quad remesh with a target-count control loop (plan §8).

Opens the P1 ``proxy.blend`` (a watertight manifold) and runs
``bpy.ops.object.quadriflow_remesh`` to a pure-quad mesh near ``T_goal`` (≈ 2,900
quads, §1 budget decision). QuadriFlow's ``target_faces`` is only a hint, so the
actual count is driven into the acceptance band by the proportional search
(:func:`~retopo_agent.geometry.target_search.search_quadriflow_target`); every
attempt is hard-asserted for the pipeline's non-negotiables:

    * 100% quads (0 triangles, 0 n-gons) — QuadriFlow emits pure quads natively;
    * 0 non-manifold edges;
    * components ≤ the proxy's component count (P1 found the statue proxy is 2
      shells — the body + the separate trident/staff — so the bound is 2, not 1).

QuadriFlow is seed-sensitive at low targets, so a failed assert bumps the seed
(up to ``max_seeds``) before the P4 retry ladder escalates. ``two_stage`` runs the
P0 decision-gate fallback (proxy → ~20k quads → T_goal), which is more stable than
one big 1M→2.9k jump.

Each attempt remeshes a *fresh duplicate* of the proxy (quadriflow_remesh is
destructive); proxy duplicates are ~1M faces (cheap vs the 24.9M source) and the
quad results are tiny, so retries are inexpensive. ``bpy`` is imported lazily.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from retopo_agent.blender.proxy import (
    _bmesh_topology,
    _make_only_active,
    _purge_orphans,
    _remove_object,
    _world_bbox_diagonal,
)
from retopo_agent.geometry.target_search import (
    quality_band,
    search_quadriflow_target,
    target_error_ratio,
)


@dataclass
class QuadRemeshResult:
    obj: object  # the accepted (or best) quad bpy object
    target_faces: int
    requested_faces: int  # the QuadriFlow target_faces request that produced obj
    seed: int
    two_stage: bool
    preserve_sharp: bool
    preserve_boundary: bool
    component_bound: int
    accepted: bool
    coverage: dict = field(default_factory=dict)
    attempts: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def metrics(self) -> dict:
        return _assess_quad_mesh(self.obj, self.target_faces, self.component_bound)

    def to_dict(self) -> dict:
        m = self.metrics
        return {
            "accepted": self.accepted,
            "target_faces": self.target_faces,
            "requested_faces": self.requested_faces,
            "seed": self.seed,
            "two_stage": self.two_stage,
            "preserve_sharp": self.preserve_sharp,
            "preserve_boundary": self.preserve_boundary,
            "component_bound": self.component_bound,
            "metrics": m,
            "coverage": self.coverage,
            "attempts": self.attempts,
            "notes": self.notes,
        }


def quad_remesh_proxy(
    proxy_obj,
    target_faces: int = 2900,
    *,
    component_bound: int | None = None,
    max_target_iter: int = 4,
    max_seeds: int = 3,
    preserve_sharp: bool = False,
    preserve_boundary: bool = False,
    two_stage: bool = False,
    intermediate_faces: int = 20000,
) -> QuadRemeshResult:
    """Remesh ``proxy_obj`` to ~``target_faces`` pure quads (plan §8).

    Runs the proportional ``target_faces`` search under each seed in turn; the
    first seed whose best attempt passes the hard asserts (pure-quad, manifold,
    component-bounded) and lands in an accepted/retry band wins. If none pass,
    returns the best attempt with ``accepted=False`` so the P4 ladder can take over
    (never silently returns a non-pure-quad mesh). ``component_bound`` defaults to
    the proxy's own component count.
    """
    import bpy  # noqa: F401

    if component_bound is None:
        component_bound = len(_bmesh_topology(proxy_obj)["component_sizes"])

    attempts: list[dict] = []
    notes: list[str] = [
        f"quadriflow target={target_faces}, component_bound={component_bound}, "
        f"preserve_sharp={preserve_sharp}, preserve_boundary={preserve_boundary}, "
        f"two_stage={two_stage}"
    ]

    # The mesh QuadriFlow's final stage runs on: the proxy directly, or a stable
    # ~20k-quad intermediate remesh of it (P0 two-stage fallback).
    stage_source = proxy_obj
    intermediate_obj = None
    if two_stage:
        intermediate_obj = _quadriflow_once(
            proxy_obj, intermediate_faces, seed=0,
            preserve_sharp=preserve_sharp, preserve_boundary=preserve_boundary,
            name="AI_Quad_stage1",
        )
        if intermediate_obj is not None:
            stage_source = intermediate_obj
            notes.append(
                f"two-stage: proxy -> {len(intermediate_obj.data.polygons)} quads "
                f"(requested {intermediate_faces})"
            )
        else:
            notes.append("two-stage stage-1 failed; falling back to single-stage on proxy")

    best: dict | None = None  # {"obj", "requested", "seed", "metrics"}

    for seed in range(max_seeds):
        candidates: dict[int, object] = {}

        def remesh(requested: int) -> int:
            obj = _quadriflow_once(
                stage_source, requested, seed=seed,
                preserve_sharp=preserve_sharp, preserve_boundary=preserve_boundary,
                name="AI_Quad_probe",
            )
            if obj is None:
                return 0
            candidates[requested] = obj
            return len(obj.data.polygons)

        search = search_quadriflow_target(remesh, target_faces, max_iter=max_target_iter)
        cand_obj = candidates.pop(search.value, None)
        for stale in candidates.values():
            _remove_object(stale)
        candidates.clear()

        if cand_obj is None:
            attempts.append({"seed": seed, "error": "all quadriflow probes failed"})
            notes.append(f"seed {seed}: quadriflow produced no usable mesh")
            continue

        metrics = _assess_quad_mesh(cand_obj, target_faces, component_bound)
        # Cheap bbox explosion guard on every probe (the only coverage check valid
        # PRE-shrinkwrap): preserve_sharp at 2.9k produced flyaway geometry with a
        # bbox 24× the proxy — pure-quad and "manifold" but garbage. The strict
        # directional max/p99 coverage is deferred to P3 (post-projection), since
        # raw QuadriFlow always pulls in at low targets (bbox 0.80 @ 2.9k → 0.99 @ 10k).
        bbox = bbox_axis_coverage(cand_obj, proxy_obj)
        metrics["bbox_coverage"] = bbox
        metrics["exploded"] = bbox["max_ratio"] > COVERAGE_BBOX_MAX_RATIO
        if metrics["exploded"]:
            metrics["passes_asserts"] = False
        attempt = {"seed": seed, "requested_faces": search.value, "iterations": search.iterations, **metrics}
        attempts.append(attempt)
        notes.append(
            f"seed {seed}: requested {search.value} -> {metrics['faces']} faces "
            f"(quad_ratio={metrics['quad_ratio']}, band={metrics['band']}, "
            f"pure_quad={metrics['pure_quad']}, non_manifold={metrics['non_manifold_edges']}, "
            f"components={metrics['components']}, passes={metrics['passes_asserts']})"
        )

        cand = {"obj": cand_obj, "requested": search.value, "seed": seed, "metrics": metrics}
        if _better(cand, best, target_faces):
            if best is not None:
                _remove_object(best["obj"])
            best = cand
        else:
            _remove_object(cand_obj)

        if metrics["passes_asserts"] and metrics["band"] in ("accepted", "retry"):
            break  # good enough; stop bumping the seed
        notes.append(f"seed {seed}: asserts/band not satisfied, bumping seed")

    if intermediate_obj is not None and (best is None or best["obj"] is not intermediate_obj):
        _remove_object(intermediate_obj)
    _purge_orphans()

    if best is None:
        raise RuntimeError("QuadriFlow produced no usable quad mesh across all seeds")

    best["obj"].name = "AI_Quad"
    best["obj"].data.name = "AI_Quad"
    metrics = best["metrics"]
    accepted = bool(metrics["passes_asserts"] and metrics["band"] in ("accepted", "retry"))

    # Full directional (proxy→quad) coverage on the chosen mesh only — recorded, NOT
    # gated here (it is the P3/P4 post-projection gate). Pre-shrinkwrap it reads
    # pessimistic by design; P3 re-projection is what closes the gap.
    coverage = coverage_report(best["obj"], proxy_obj)
    notes.append(
        f"coverage (proxy->quad, pre-shrinkwrap, INFORMATIONAL): bbox_min={coverage['bbox']['min_ratio']} "
        f"max_ratio={coverage['proxy_to_quad'].get('max_ratio')} "
        f"p99_ratio={coverage['proxy_to_quad'].get('p99_ratio')} -> "
        f"{'meets P3-gate already' if coverage['passes'] else 'expected to need P3 re-projection'}"
    )
    return QuadRemeshResult(
        obj=best["obj"],
        target_faces=target_faces,
        requested_faces=best["requested"],
        seed=best["seed"],
        two_stage=two_stage,
        preserve_sharp=preserve_sharp,
        preserve_boundary=preserve_boundary,
        component_bound=component_bound,
        accepted=accepted,
        coverage=coverage,
        attempts=attempts,
        notes=notes,
    )


def _quadriflow_once(source_obj, target_faces: int, *, seed: int, preserve_sharp: bool,
                     preserve_boundary: bool, name: str):
    """One destructive QuadriFlow remesh on a fresh duplicate of ``source_obj``.
    Returns the new quad object, or ``None`` if the operator failed."""
    import bpy

    dup = source_obj.copy()
    dup.data = source_obj.data.copy()
    dup.name = name
    dup.data.name = name
    bpy.context.collection.objects.link(dup)
    _make_only_active(dup)
    try:
        bpy.ops.object.quadriflow_remesh(
            target_faces=int(target_faces),
            use_mesh_symmetry=False,
            use_preserve_sharp=bool(preserve_sharp),
            use_preserve_boundary=bool(preserve_boundary),
            seed=int(seed),
            mode="FACES",
        )
    except RuntimeError:
        _remove_object(dup)
        return None
    if not dup.data.polygons:
        _remove_object(dup)
        return None
    return dup


def _assess_quad_mesh(obj, target_faces: int, component_bound: int) -> dict:
    """Face-type / manifold / component metrics + the P2 hard-assert verdict."""
    import numpy as np

    mesh = obj.data
    n = len(mesh.polygons)
    loop_totals = np.empty(n, dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", loop_totals)
    tris = int(np.count_nonzero(loop_totals == 3))
    quads = int(np.count_nonzero(loop_totals == 4))
    ngons = int(n - tris - quads)

    topo = _bmesh_topology(obj)
    components = len(topo["component_sizes"])
    non_manifold = topo["non_manifold_edges"]

    pure_quad = n > 0 and tris == 0 and ngons == 0
    passes = bool(pure_quad and non_manifold == 0 and components <= component_bound)
    return {
        "faces": n,
        "tris": tris,
        "quads": quads,
        "ngons": ngons,
        "quad_ratio": round(quads / n, 4) if n else 0.0,
        "non_manifold_edges": non_manifold,
        "boundary_edges": topo["boundary_edges"],
        "components": components,
        "target_error_ratio": round(target_error_ratio(n, target_faces), 4),
        "band": quality_band(n, target_faces),
        "pure_quad": pure_quad,
        "passes_asserts": passes,
    }


# Coverage gate thresholds (plan §8.3, directional). Calibrated from the P2 coverage
# experiment (scripts/spike_p2_coverage.py) — see the §8 P2-coverage results block.
# Directional = proxy→quad: catches geometry the quad MISSED, which the quad→proxy
# fidelity (P1) can never see. bbox per-axis coverage is the cheap first screen.
COVERAGE_BBOX_MIN_RATIO = 0.99      # quad must reach ≥99% of the proxy's per-axis extent
COVERAGE_BBOX_MAX_RATIO = 1.03      # ...and NOT overshoot it (catches flyaway/exploded geo,
                                    # e.g. preserve_sharp at 2.9k produced bbox 24× the proxy)
COVERAGE_MAX_RATIO = 0.05           # worst proxy→quad distance, as a fraction of bbox diag
COVERAGE_P99_RATIO = 0.025          # 99th-pct proxy→quad distance / bbox diag


def bbox_axis_coverage(result_obj, ref_obj) -> dict:
    """Cheap first-screen coverage: per-axis world bbox extent of ``result_obj``
    vs ``ref_obj``. A thin feature that defines an extreme (a trident prong held
    high) shrinks the bbox along its axis when lost, so a per-axis ratio < 1
    flags it in O(verts) with no BVH. Pre-shrinkwrap the quad sits ~½ voxel
    inside the proxy, so the ≥0.99 gate is deliberately loose."""
    from mathutils import Vector

    def bounds(o):
        cs = [o.matrix_world @ Vector(c) for c in o.bound_box]
        lo = [min(c[i] for c in cs) for i in range(3)]
        hi = [max(c[i] for c in cs) for i in range(3)]
        return lo, hi

    rlo, rhi = bounds(result_obj)
    plo, phi = bounds(ref_obj)
    per_axis = {}
    for i, ax in enumerate("xyz"):
        ref_ext = phi[i] - plo[i]
        res_ext = rhi[i] - rlo[i]
        per_axis[ax] = round(res_ext / ref_ext, 4) if ref_ext > 1e-9 else 1.0
    return {"per_axis": per_axis, "min_ratio": min(per_axis.values()),
            "max_ratio": max(per_axis.values())}


def directional_coverage(ref_obj, result_obj, *, max_vert_samples: int = 20000,
                         max_face_samples: int = 8000) -> dict:
    """Distance from ``ref_obj`` surface samples to the NEAREST point on
    ``result_obj`` (BVH on ``result_obj``). This is the **proxy→quad** direction:
    if the quad dropped a feature the proxy had, the proxy samples on that feature
    are far from the quad → large max/p99. The opposite direction (quad→proxy,
    the P1 fidelity) is blind to exactly this. Both meshes share world space."""
    import numpy as np
    import bpy  # noqa: F401
    from mathutils.bvhtree import BVHTree

    depsgraph = bpy.context.evaluated_depsgraph_get()
    tree = BVHTree.FromObject(result_obj, depsgraph)
    to_local = result_obj.matrix_world.inverted_safe() @ ref_obj.matrix_world
    diag = _world_bbox_diagonal(ref_obj)

    def stride(n: int, k: int):
        if n <= k:
            return range(n)
        step = n / k
        return (int(i * step) for i in range(k))

    distances: list[float] = []
    verts = ref_obj.data.vertices
    for vi in stride(len(verts), max_vert_samples):
        loc, nrm, idx, d = tree.find_nearest(to_local @ verts[vi].co)
        if d is not None:
            distances.append(float(d))
    polys = ref_obj.data.polygons
    for fi in stride(len(polys), max_face_samples):
        loc, nrm, idx, d = tree.find_nearest(to_local @ polys[fi].center)
        if d is not None:
            distances.append(float(d))

    if not distances:
        return {"sample_count": 0}
    arr = np.asarray(distances, dtype=float)
    p50, p90, p99 = (float(x) for x in np.percentile(arr, [50, 90, 99]))
    mx = float(arr.max())
    return {
        "sample_count": int(arr.size),
        "bbox_diagonal": round(diag, 4),
        "mean": round(float(arr.mean()), 4),
        "p50": round(p50, 4),
        "p90": round(p90, 4),
        "p99": round(p99, 4),
        "max": round(mx, 4),
        "p99_ratio": round(p99 / diag, 5) if diag else 0.0,
        "max_ratio": round(mx / diag, 5) if diag else 0.0,
    }


def coverage_report(quad_obj, proxy_obj, *, bbox_min_ratio: float = COVERAGE_BBOX_MIN_RATIO,
                    max_ratio: float = COVERAGE_MAX_RATIO,
                    p99_ratio: float = COVERAGE_P99_RATIO) -> dict:
    """Full directional coverage gate for a quad result vs its proxy (plan §8.3).
    ``passes`` requires the cheap per-axis bbox screen AND the proxy→quad max/p99
    distance ratios under their thresholds — i.e. the quad both *reaches* the
    proxy's extent and has no proxy region left far from any quad face."""
    bbox = bbox_axis_coverage(quad_obj, proxy_obj)
    dirn = directional_coverage(proxy_obj, quad_obj)
    passes = (
        bbox["min_ratio"] >= bbox_min_ratio
        and dirn.get("max_ratio", 1.0) <= max_ratio
        and dirn.get("p99_ratio", 1.0) <= p99_ratio
    )
    return {
        "bbox": bbox,
        "proxy_to_quad": dirn,
        "thresholds": {"bbox_min_ratio": bbox_min_ratio, "max_ratio": max_ratio, "p99_ratio": p99_ratio},
        "passes": bool(passes),
    }


def _better(cand: dict, best: dict | None, target_faces: int) -> bool:
    """Prefer a candidate that passes the hard asserts; among equals, the closer
    face count to target wins. Keeps the best fallback when nothing passes."""
    if best is None:
        return True
    cm, bm = cand["metrics"], best["metrics"]
    if cm["passes_asserts"] != bm["passes_asserts"]:
        return cm["passes_asserts"]
    return cm["target_error_ratio"] < bm["target_error_ratio"]
