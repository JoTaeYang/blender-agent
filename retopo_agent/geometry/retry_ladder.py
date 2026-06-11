"""Progressive decimation retry ladder (Decimation plan DM5, §8).

When the primary Collapse pass plateaus (DM1) the right move is *not* to jump
straight to a voxel/cluster remesh -- it is to try more aggressive strategies
within the same triangle-decimation family before giving up on topology. This
module is that ladder:

    Attempt 1: collapse + full feature protection
    Attempt 2: collapse + relaxed feature protection
    Attempt 3: cleanup constraints + collapse
    Attempt 4: flat-region planar reduction + collapse
    Attempt 5: component budget policy + collapse
    Attempt 6: custom QEM triangle collapse        (DM6 -- skipped until implemented)

Two pieces, both pure and unit-tested offline:

- :func:`run_retry_ladder` is the **driver**: it walks the ladder, records a
  report per attempt explaining why the target was (not) met, keeps escalating
  while the shape stays acceptable, and -- the DM5/DM7 rule -- **rolls back to the
  last shape-accepted attempt the moment an attempt breaks the shape**. It takes
  an ``execute(spec)`` callable, so the orchestration is testable with synthetic
  attempts and reused by the Blender adapter unchanged.
- :func:`make_attempt_executor` builds a concrete ``execute`` that runs each
  strategy as a pure-geometry transform on a :class:`MeshGraph` (feature-aware
  cluster decimation, weld cleanup, planar reduction, DM3 component removal) and
  scores it with :func:`~retopo_agent.geometry.shape_eval.evaluate_shape_match`.

The Blender adapter is :mod:`retopo_agent.blender.retry_ladder`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from retopo_agent.geometry.component_budget import analyze_components
from retopo_agent.geometry.decimate import (
    _rebuild_from_clusters,
    bounding_box_diagonal,
    decimate_to_target,
    feature_aware_decimate_to_target,
)
from retopo_agent.geometry.diagnosis import DEFAULT_TINY_FACE_FRACTION
from retopo_agent.geometry.features import feature_vertex_mask
from retopo_agent.geometry.shape_eval import DECIMATION_SHAPE_THRESHOLDS, evaluate_shape_match
from retopo_agent.geometry.target_search import quality_band, target_error_ratio
from uv_agent.geometry.mesh_graph import MeshGraph

import numpy as np

# Ladder method names (plan §8 retry ladder).
METHOD_COLLAPSE_FULL = "collapse_full_feature_protection"
METHOD_COLLAPSE_RELAXED = "collapse_relaxed_feature_protection"
METHOD_CLEANUP = "cleanup_then_collapse"
METHOD_PLANAR = "planar_flat_region_reduce_then_collapse"
METHOD_COMPONENT = "component_budget_then_collapse"
METHOD_QEM = "custom_qem_triangle_collapse"

_SHAPE_ACCEPTABLE = {"accepted", "retry"}
_BAND_RANK = {"accepted": 0, "retry": 1, "failed": 2}


@dataclass
class AttemptSpec:
    """One rung of the ladder: ``attempt`` number, ``method`` name, and the
    ``params`` the executor reads to run that strategy."""

    attempt: int
    method: str
    params: dict = field(default_factory=dict)


# The default ladder, in escalating order (plan §8).
LADDER: list[AttemptSpec] = [
    AttemptSpec(1, METHOD_COLLAPSE_FULL, {"strategy": "feature_collapse", "protection": "full"}),
    AttemptSpec(2, METHOD_COLLAPSE_RELAXED, {"strategy": "feature_collapse", "protection": "relaxed"}),
    AttemptSpec(3, METHOD_CLEANUP, {"strategy": "cleanup"}),
    AttemptSpec(4, METHOD_PLANAR, {"strategy": "planar"}),
    AttemptSpec(5, METHOD_COMPONENT, {"strategy": "component"}),
    AttemptSpec(6, METHOD_QEM, {"strategy": "qem"}),
]


@dataclass
class AttemptResult:
    """Outcome of one ladder attempt (plan §8 per-attempt report)."""

    attempt: int
    method: str
    input_faces: int
    actual_faces: int
    shape_status: str
    target_band: str
    note: str = ""
    mesh: MeshGraph | None = field(default=None, repr=False)  # candidate; not serialized

    def to_dict(self) -> dict:
        return {
            "attempt": self.attempt,
            "method": self.method,
            "input_faces": self.input_faces,
            "actual_faces": self.actual_faces,
            "shape_status": self.shape_status,
            "target_band": self.target_band,
            "note": self.note,
        }


@dataclass
class LadderResult:
    """The full ladder run (plan §8 / DM7 ``decimation_attempts.json``)."""

    attempts: list[AttemptResult]
    selected_attempt: int | None
    selection_reason: str
    target_face_count: int

    @property
    def selected(self) -> AttemptResult | None:
        for a in self.attempts:
            if a.attempt == self.selected_attempt:
                return a
        return None

    def to_dict(self) -> dict:
        return {
            "selected_attempt": self.selected_attempt,
            "selection_reason": self.selection_reason,
            "target_face_count": self.target_face_count,
            "attempts": [a.to_dict() for a in self.attempts],
        }


def run_retry_ladder(execute, *, target_face_count: int, ladder: list[AttemptSpec] | None = None) -> LadderResult:
    """Drive the retry ladder, applying the DM5/DM7 shape-aware policy (plan §8/§10).

    ``execute(spec) -> AttemptResult | None`` runs one attempt (``None`` = the
    attempt is unavailable, e.g. the DM6 QEM step before it exists -- it is skipped).
    Policy, worst-to-best:

    - ``shape failed`` -> stop and roll back to the last shape-accepted attempt
      (the most aggressive one that did not break the shape);
    - ``target accepted`` with shape accepted -> success, stop;
    - ``target accepted`` with shape retry -> warning success, stop;
    - otherwise (target not yet met, shape still acceptable) -> record and keep
      escalating to the next, more aggressive attempt.

    The selected attempt is the best shape-accepted one (closest to target); the
    ``selection_reason`` always explains the outcome.
    """
    ladder = ladder if ladder is not None else LADDER
    attempts: list[AttemptResult] = []
    best_accepted: AttemptResult | None = None  # shape-accepted, closest to target
    selected: AttemptResult | None = None
    reason = ""

    def closer(a: AttemptResult, b: AttemptResult) -> bool:
        return target_error_ratio(a.actual_faces, target_face_count) < target_error_ratio(
            b.actual_faces, target_face_count
        )

    finished = False
    for spec in ladder:
        res = execute(spec)
        if res is None:
            continue
        attempts.append(res)

        if res.shape_status == "failed":
            if best_accepted is not None:
                selected = best_accepted
                reason = (
                    f"shape failed at attempt {res.attempt} ({res.method}); "
                    f"rolled back to attempt {best_accepted.attempt}"
                )
            else:
                selected = res
                reason = (
                    f"shape failed at attempt {res.attempt} with no prior shape-accepted "
                    "attempt; kept as best effort"
                )
            finished = True
            break

        if res.target_band == "accepted":
            selected = res
            reason = (
                "target accepted and shape accepted"
                if res.shape_status == "accepted"
                else "target accepted and shape retry (warning success)"
            )
            if res.shape_status == "accepted" and (best_accepted is None or closer(res, best_accepted)):
                best_accepted = res
            finished = True
            break

        # Target not met yet, shape still acceptable -> candidate; keep escalating.
        if res.shape_status == "accepted" and (best_accepted is None or closer(res, best_accepted)):
            best_accepted = res

    if not finished:
        if best_accepted is not None:
            selected = best_accepted
            reason = "best effort: most aggressive shape-accepted attempt; target not reached"
        elif attempts:
            selected = min(attempts, key=lambda a: (_BAND_RANK[a.target_band], a.attempt))
            reason = "best effort: no shape-accepted attempt; kept closest-to-target attempt"
        else:
            reason = "no attempts ran"

    return LadderResult(
        attempts=attempts,
        selected_attempt=selected.attempt if selected else None,
        selection_reason=reason,
        target_face_count=target_face_count,
    )


# -- concrete pure-geometry attempt strategies -----------------------------


def _weld_and_clean(mesh: MeshGraph, *, tol_fraction: float = 1e-5) -> MeshGraph:
    """Merge near-duplicate vertices (within ``tol_fraction`` of the bbox diagonal)
    and drop the degenerate / duplicate faces that creates -- the "cleanup
    constraints" of attempt 3. Reuses the cluster rebuild so the face cleanup is
    identical to the decimator's."""
    co = np.asarray([v.co for v in mesh.vertices], dtype=float)
    oid = f"{mesh.object_id}_clean"
    if len(co) == 0 or mesh.face_count == 0:
        return MeshGraph.from_faces(oid, [], [])
    diag = bounding_box_diagonal(mesh)
    tol = max(tol_fraction * diag, 1e-12)
    lo = co.min(axis=0)
    quant = np.round((co - lo) / tol).astype(np.int64)
    keys = [tuple(int(c) for c in row) for row in quant]
    members: dict[tuple, list[int]] = {}
    for vid, key in enumerate(keys):
        members.setdefault(key, []).append(vid)
    sorted_keys = sorted(members)
    cluster_of_key = {key: i for i, key in enumerate(sorted_keys)}
    new_vertices = [tuple(co[members[key]].mean(axis=0)) for key in sorted_keys]
    vertex_cluster = np.fromiter((cluster_of_key[k] for k in keys), dtype=np.int64, count=len(keys))
    return _rebuild_from_clusters(mesh, vertex_cluster, new_vertices, oid)


def _drop_tiny_components(mesh: MeshGraph, *, tiny_face_fraction: float) -> tuple[MeshGraph, int, int]:
    """Remove tiny detached components (DM3 ``budget`` removal), keeping at least the
    largest. Returns ``(reduced_mesh, removed_component_count, removed_face_count)``."""
    comps = analyze_components(mesh, tiny_face_fraction=tiny_face_fraction)
    if not comps:
        return mesh, 0, 0
    keep = [c for c in comps if not c.is_tiny] or [comps[0]]
    keep_ids = {c.id for c in keep}
    removed = [c for c in comps if c.id not in keep_ids]
    if not removed:
        return mesh, 0, 0
    keep_fids = sorted({fid for c in keep for fid in c.face_ids})
    old_faces = [mesh.faces[fid] for fid in keep_fids]
    used_v = sorted({vid for f in old_faces for vid in f.vertex_ids})
    remap = {vid: i for i, vid in enumerate(used_v)}
    verts = [mesh.vertices[vid].co for vid in used_v]
    faces = [[remap[vid] for vid in f.vertex_ids] for f in old_faces]
    mats = [f.material_index for f in old_faces]
    reduced = MeshGraph.from_faces(f"{mesh.object_id}_nbudget", verts, faces, material_indices=mats)
    return reduced, len(removed), sum(c.face_count for c in removed)


def _run_strategy(
    spec: AttemptSpec,
    base: MeshGraph,
    target: int,
    *,
    feature_angle: float,
    relaxed_angle: float,
    allow_component_removal: bool,
    tiny_face_fraction: float,
) -> tuple[MeshGraph | None, str]:
    """Execute one ladder strategy on ``base`` -> ``(candidate_mesh, note)``.
    ``candidate`` is ``None`` for an unavailable strategy (the QEM step)."""
    strategy = spec.params.get("strategy")
    if strategy == "qem":
        return None, "custom QEM triangle collapse not implemented yet (DM6)"

    if strategy == "feature_collapse":
        angle = feature_angle if spec.params.get("protection") == "full" else relaxed_angle
        mask = feature_vertex_mask(base, angle_threshold=angle)
        low = feature_aware_decimate_to_target(base, target, mask).low_mesh
        return low, f"feature-aware collapse (protect >= {angle:g} deg, {int(mask.sum())} verts kept)"

    if strategy == "cleanup":
        cleaned = _weld_and_clean(base)
        mask = feature_vertex_mask(cleaned, angle_threshold=relaxed_angle)
        low = feature_aware_decimate_to_target(cleaned, target, mask).low_mesh
        return low, (
            f"cleanup welded {base.vertex_count - cleaned.vertex_count} verts / dropped "
            f"{base.face_count - cleaned.face_count} faces, then collapse"
        )

    if strategy == "planar":
        low = decimate_to_target(base, target).low_mesh
        return low, "planar flat-region reduction (no feature protection)"

    if strategy == "component":
        if allow_component_removal:
            reduced, n_removed, f_removed = _drop_tiny_components(base, tiny_face_fraction=tiny_face_fraction)
            prefix = f"removed {n_removed} tiny components ({f_removed} faces), then "
        else:
            reduced, prefix = base, "component removal off (preserve_all), "
        low = decimate_to_target(reduced, target).low_mesh
        return low, prefix + "collapse"

    return None, f"unknown strategy {strategy!r}"


def make_attempt_executor(
    base_mesh: MeshGraph,
    target_face_count: int,
    *,
    reference_mesh: MeshGraph | None = None,
    shape_thresholds=DECIMATION_SHAPE_THRESHOLDS,
    feature_angle: float = 30.0,
    relaxed_angle: float = 60.0,
    allow_component_removal: bool = False,
    tiny_face_fraction: float = DEFAULT_TINY_FACE_FRACTION,
):
    """Build an ``execute(spec)`` for :func:`run_retry_ladder` that runs each
    strategy as a pure-geometry transform on ``base_mesh`` and scores its shape
    against ``reference_mesh`` (defaults to ``base_mesh`` -- "how much did this
    attempt degrade the already-accepted plateau result?").

    The returned candidate ``MeshGraph`` is carried on the :class:`AttemptResult`
    so the caller can keep the selected one.
    """
    reference = reference_mesh if reference_mesh is not None else base_mesh

    def execute(spec: AttemptSpec) -> AttemptResult | None:
        candidate, note = _run_strategy(
            spec, base_mesh, target_face_count,
            feature_angle=feature_angle, relaxed_angle=relaxed_angle,
            allow_component_removal=allow_component_removal, tiny_face_fraction=tiny_face_fraction,
        )
        if candidate is None:
            return None
        shape = evaluate_shape_match(reference, candidate, thresholds=shape_thresholds)
        band = quality_band(candidate.face_count, target_face_count)
        if band != "accepted":
            note += f"; target {target_face_count} missed ({candidate.face_count} faces, band={band})"
        return AttemptResult(
            attempt=spec.attempt,
            method=spec.method,
            input_faces=base_mesh.face_count,
            actual_faces=candidate.face_count,
            shape_status=shape.status,
            target_band=band,
            note=note,
            mesh=candidate,
        )

    return execute
