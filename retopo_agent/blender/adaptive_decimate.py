"""Phase A2 — adaptive decimation on the manifold proxy (Adaptive Low-Poly plan §5).

This is the **default** low-poly generator (the v2 pivot away from uniform pure-quad
QuadriFlow). It runs Decimate (Collapse) *on the P1 proxy* — a clean watertight
manifold 1M-face mesh — so QEM produces the requested adaptive distribution: large
polygons on flat regions, small on detail, a natural tri/quad mix once A3 merges
coplanar triangle pairs. The earlier decimation effort plateaued only because it fed
raw non-manifold ZBrush soup into Collapse; the proxy eliminates that input problem
(plan §0).

Scope of A2 (plan §5):

    open proxy.blend  →  Decimate (COLLAPSE) + ratio search → T_goal ±10%
                      →  optional feature-protection vertex group (retry rung)
                      →  optional shrinkwrap (NEAREST) snap back to proxy
                      →  record per-attempt geometry + coverage + shape metrics

The ratio-search + feature-protection core is reused from the validated
:func:`retopo_agent.blender.decimate.generate_decimated_object` (it duplicates the
source, searches the Collapse ratio, builds the feature/importance vertex group, and
triangulates the collapse). A2 adds the proxy-specific steps on top: the shrinkwrap
snap (Collapse places verts at QEM-optimal positions slightly off-surface; one
nearest-point snap restores contact, kept only if it measurably improves shape) and
the full per-attempt metric record (faces, tri/quad/n-gon split, manifoldness,
component count, per-axis bbox coverage, directional proxy→low distances, wall time)
that A4's silhouette gate consumes.

The decision logic that does not need Blender — the tri/quad/n-gon split from a
loop-total array, and the "keep the shrinkwrap only if it improved shape" rule — is
factored into pure helpers (:func:`face_type_breakdown`, :func:`shrinkwrap_improves`)
so it is unit-tested offline. Everything that touches ``bpy`` is lazy-imported.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from retopo_agent.geometry.target_search import quality_band, target_error_ratio

SHRINKWRAP_MODIFIER = "AI_Adaptive_Shrinkwrap"


def face_type_breakdown(loop_totals) -> dict:
    """Split a per-face loop-total sequence into tri / quad / n-gon counts.

    ``loop_totals`` is the ``polygon.loop_total`` array (3 = triangle, 4 = quad,
    ≥5 = n-gon). Pure so the adaptive-mode invariant — **0 n-gons (hard)** and a
    natural tri/quad mix — is asserted in unit tests without Blender. Returns the
    counts plus the quad share of the mesh (organic, NOT a target to chase)."""
    tris = quads = ngons = 0
    for lt in loop_totals:
        n = int(lt)
        if n == 3:
            tris += 1
        elif n == 4:
            quads += 1
        elif n >= 5:
            ngons += 1
    faces = tris + quads + ngons
    return {
        "faces": faces,
        "tris": tris,
        "quads": quads,
        "ngons": ngons,
        "quad_ratio": round(quads / faces, 4) if faces else 0.0,
    }


def shrinkwrap_improves(before_mean_dist: float, after_mean_dist: float, *,
                        rel_tol: float = 1e-4) -> bool:
    """Whether the post-collapse shrinkwrap snap should be kept (plan §5.4).

    Collapse leaves verts slightly off-surface; a NEAREST shrinkwrap snaps them
    back. We keep it ONLY if the mean low→proxy surface distance actually dropped
    (a real improvement beyond ``rel_tol`` of the pre-snap distance), so a snap
    that made the shape *worse* — or did nothing — is discarded rather than shipped
    on faith."""
    if before_mean_dist <= 0.0:
        return False
    return after_mean_dist < before_mean_dist * (1.0 - rel_tol)


def cleanup_asserts(breakdown: dict, topo: dict, *, target_face_count: int,
                    band_tol: float = 0.10, component_bound: int = 1) -> dict:
    """The §6.2 post-cleanup hard asserts, as pure logic (testable offline).

    After A3's ``tris→quads`` + degenerate cleanup the mesh must still satisfy:
    **0 n-gons** (hard), **0 non-manifold edges** (hard), components ≤
    ``component_bound`` (the proxy's single meaningful shell), and the face count
    must not have drifted out of the T_goal ±``band_tol`` band the cleanup can move
    it through. Returns each assert's pass flag plus an ``all_ok`` summary; the
    Blender cleanup raises on ``all_ok is False`` so a broken mesh never advances."""
    faces = int(breakdown.get("faces", 0))
    ngons_ok = int(breakdown.get("ngons", 0)) == 0
    non_manifold_ok = int(topo.get("non_manifold_edges", 0)) == 0
    components_ok = int(topo.get("components", 0)) <= component_bound
    band_ok = target_error_ratio(faces, target_face_count) <= band_tol
    return {
        "ngons_ok": ngons_ok,
        "non_manifold_ok": non_manifold_ok,
        "components_ok": components_ok,
        "band_ok": band_ok,
        "all_ok": bool(ngons_ok and non_manifold_ok and components_ok and band_ok),
    }


@dataclass
class AdaptiveAttempt:
    """One A2 attempt's full metric record (plan §5.5) — what A4's gate reads."""

    label: str
    faces: int
    tris: int
    quads: int
    ngons: int
    non_manifold_edges: int
    components: int
    vertex_count: int = 0
    bbox_per_axis: dict = field(default_factory=dict)
    bbox_min_ratio: float = 0.0
    proxy_to_low: dict = field(default_factory=dict)
    low_to_proxy: dict = field(default_factory=dict)
    shrinkwrap_applied: bool = False
    preserve_features: bool = False
    feature_vertex_count: int = 0
    wall_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "faces": self.faces,
            "tris": self.tris,
            "quads": self.quads,
            "ngons": self.ngons,
            "quad_ratio": round(self.quads / self.faces, 4) if self.faces else 0.0,
            "non_manifold_edges": self.non_manifold_edges,
            "components": self.components,
            "vertex_count": self.vertex_count,
            "bbox_per_axis": self.bbox_per_axis,
            "bbox_min_ratio": self.bbox_min_ratio,
            "proxy_to_low": self.proxy_to_low,
            "low_to_proxy": self.low_to_proxy,
            "shrinkwrap_applied": self.shrinkwrap_applied,
            "preserve_features": self.preserve_features,
            "feature_vertex_count": self.feature_vertex_count,
            "wall_s": self.wall_s,
            "notes": list(self.notes),
        }

    def gate_metrics(self) -> dict:
        """The flat metrics dict :func:`retopo_agent.geometry.adaptive_gate.evaluate_gate`
        consumes (a re-key of this record into the gate's expected field names)."""
        return {
            "ngons": self.ngons,
            "non_manifold_edges": self.non_manifold_edges,
            "faces": self.faces,
            "vertex_count": self.vertex_count,
            "bbox_per_axis": self.bbox_per_axis,
            "proxy_to_low": self.proxy_to_low,
            "low_to_proxy": self.low_to_proxy,
        }


@dataclass
class AdaptiveDecimateResult:
    """Outcome of :func:`adaptive_decimate_proxy` — the low-poly object plus the
    attempt record A3/A4 consume. ``band`` / ``target_error_ratio`` are the
    face-count tracking verdict; the silhouette HARD gate lives in A4."""

    obj: object  # the new low-poly bpy object
    target_face_count: int
    source_face_count: int
    ratio: float
    attempt: AdaptiveAttempt
    stopped_reason: str = ""
    plateau_face_count: int | None = None
    search_iterations: int = 0
    search_history: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def actual_face_count(self) -> int:
        return len(self.obj.data.polygons)

    @property
    def target_error_ratio(self) -> float:
        return target_error_ratio(self.actual_face_count, self.target_face_count)

    @property
    def band(self) -> str:
        return quality_band(self.actual_face_count, self.target_face_count)

    def to_dict(self) -> dict:
        return {
            "target_face_count": self.target_face_count,
            "source_face_count": self.source_face_count,
            "actual_face_count": self.actual_face_count,
            "target_error_ratio": round(self.target_error_ratio, 4),
            "band": self.band,
            "ratio": round(self.ratio, 6),
            "stopped_reason": self.stopped_reason,
            "plateau_face_count": self.plateau_face_count,
            "search_iterations": self.search_iterations,
            "search_history": self.search_history,
            "attempt": self.attempt.to_dict(),
            "notes": list(self.notes),
        }


def adaptive_decimate_proxy(
    proxy_obj,
    target_face_count: int,
    *,
    preserve_features: bool = False,
    feature_angle: float = 30.0,
    preserve_features_strength: float = 1.0,
    importance_weights=None,
    shrinkwrap: bool = True,
    max_iter: int = 8,
) -> AdaptiveDecimateResult:
    """Adaptively decimate the manifold ``proxy_obj`` toward ``target_face_count``
    (plan §5).

    Reduces a duplicate of the proxy with Decimate (Collapse), searching the
    collapse ``ratio`` until the face count lands in the T_goal ±10% band (the
    proxy is manifold, so clean convergence is expected — plateau detection is
    retained from the search as an alert, not a workaround). ``triangulate`` is on
    so the collapse never emits degenerate quads; the natural tri/quad mix is A3's
    ``tris→quads`` pass, not A2's.

    Feature protection is OFF by default (plan §5.2: QEM alone preserves silhouettes
    well on manifold input — measure first, then enable as a retry rung). Pass
    ``preserve_features=True`` (hard-edge + boundary verts) or ``importance_weights``
    (a graded importance map) to protect extremities that thin out.

    When ``shrinkwrap`` is on, a NEAREST shrinkwrap back to the proxy snaps the
    QEM-optimal verts onto the surface; it is kept ONLY if it improves the mean
    low→proxy distance (:func:`shrinkwrap_improves`), else reverted.

    The returned :class:`AdaptiveDecimateResult` carries the low-poly object and a
    full :class:`AdaptiveAttempt` metric record (geometry + coverage + shape) for
    A4's silhouette gate.
    """
    from retopo_agent.blender.decimate import generate_decimated_object
    from retopo_agent.blender.quadremesh import bbox_axis_coverage, directional_coverage

    t0 = time.monotonic()
    notes: list[str] = []

    # --- Collapse + ratio search + feature protection (reused, validated core).
    dec = generate_decimated_object(
        proxy_obj,
        target_face_count,
        triangulate=True,  # plan §5.3: avoids degenerate quads during collapse
        preserve_features=preserve_features,
        feature_angle=feature_angle,
        preserve_features_strength=preserve_features_strength,
        importance_weights=importance_weights,
        max_iter=max_iter,
    )
    low = dec.obj
    notes.extend(dec.notes)

    # --- Shrinkwrap (NEAREST) snap back to the proxy, kept only if it helps (§5.4).
    shrinkwrap_applied = False
    if shrinkwrap and len(low.data.polygons) > 0:
        shrinkwrap_applied = _maybe_shrinkwrap_to_proxy(low, proxy_obj, notes)

    # --- Full per-attempt metric record (§5.5): geometry + coverage + shape.
    breakdown = _mesh_face_breakdown(low)
    topo = _mesh_topology(low)
    bbox = bbox_axis_coverage(low, proxy_obj)
    proxy_to_low = directional_coverage(proxy_obj, low)
    low_to_proxy = _low_to_proxy_shape(low, proxy_obj)

    attempt = AdaptiveAttempt(
        label=f"adaptive_t{target_face_count}"
        + ("_feat" if (preserve_features or importance_weights is not None) else "")
        + ("_sw" if shrinkwrap_applied else ""),
        faces=breakdown["faces"],
        tris=breakdown["tris"],
        quads=breakdown["quads"],
        ngons=breakdown["ngons"],
        non_manifold_edges=topo["non_manifold_edges"],
        components=topo["components"],
        vertex_count=len(low.data.vertices),
        bbox_per_axis=bbox["per_axis"],
        bbox_min_ratio=bbox["min_ratio"],
        proxy_to_low=proxy_to_low,
        low_to_proxy=low_to_proxy,
        shrinkwrap_applied=shrinkwrap_applied,
        preserve_features=dec.preserve_features,
        feature_vertex_count=dec.feature_vertex_count,
        wall_s=round(time.monotonic() - t0, 2),
        notes=list(notes),
    )

    return AdaptiveDecimateResult(
        obj=low,
        target_face_count=target_face_count,
        source_face_count=dec.source_face_count,
        ratio=dec.ratio,
        attempt=attempt,
        stopped_reason=dec.stopped_reason,
        plateau_face_count=dec.plateau_face_count,
        search_iterations=dec.search_iterations,
        search_history=dec.search_history,
        notes=notes,
    )


def _maybe_shrinkwrap_to_proxy(low_obj, proxy_obj, notes: list[str]) -> bool:
    """Apply a NEAREST shrinkwrap of ``low_obj`` onto ``proxy_obj``, measuring the
    mean low→proxy surface distance before and after, and keep the snap only if it
    improved (plan §5.4). Returns whether the snap was kept. On any failure the
    mesh is left as it was."""
    import bpy
    from retopo_agent.blender.retopo import _make_only_active
    from retopo_agent.blender.shape import evaluate_shape_match_blender
    from retopo_agent.geometry.shape_eval import DECIMATION_SHAPE_THRESHOLDS

    try:
        before = evaluate_shape_match_blender(
            proxy_obj, low_obj, thresholds=DECIMATION_SHAPE_THRESHOLDS
        ).surface_distance_mean
    except Exception as exc:  # noqa: BLE001 - shape eval is best-effort guidance
        notes.append(f"shrinkwrap skipped (pre-measure failed: {exc})")
        return False

    # Snapshot the pre-snap mesh so a non-improving snap can be reverted exactly.
    backup = low_obj.data.copy()
    try:
        _make_only_active(low_obj)
        mod = low_obj.modifiers.new(name=SHRINKWRAP_MODIFIER, type="SHRINKWRAP")
        mod.wrap_method = "NEAREST_SURFACEPOINT"
        mod.target = proxy_obj
        bpy.ops.object.modifier_apply(modifier=mod.name)
    except (RuntimeError, AttributeError) as exc:
        notes.append(f"shrinkwrap failed ({exc}); kept un-snapped mesh")
        low_obj.data = backup
        return False

    try:
        after = evaluate_shape_match_blender(
            proxy_obj, low_obj, thresholds=DECIMATION_SHAPE_THRESHOLDS
        ).surface_distance_mean
    except Exception as exc:  # noqa: BLE001
        after = before  # treat as no improvement -> revert below
        notes.append(f"shrinkwrap post-measure failed ({exc}); reverting")

    if shrinkwrap_improves(before, after):
        notes.append(f"shrinkwrap kept: mean dist {before:.5g} -> {after:.5g}")
        if backup.users == 0:
            bpy.data.meshes.remove(backup)
        return True

    snapped = low_obj.data
    low_obj.data = backup  # revert to the pre-snap mesh
    if snapped.users == 0:
        bpy.data.meshes.remove(snapped)
    notes.append(f"shrinkwrap discarded: mean dist {before:.5g} -> {after:.5g} (no gain)")
    return False


def _mesh_face_breakdown(obj) -> dict:
    """Tri/quad/n-gon split of a bpy mesh via :func:`face_type_breakdown`."""
    import numpy as np

    polys = obj.data.polygons
    loop_totals = np.empty(len(polys), dtype=np.int32)
    polys.foreach_get("loop_total", loop_totals)
    return face_type_breakdown(loop_totals)


def _low_to_proxy_shape(low_obj, proxy_obj) -> dict:
    """Mean low→proxy surface distance + normal deviation (the SOFT shape metrics
    A4's gate compares to the reference baseline). Best-effort: returns ``{}`` if
    the BVH shape eval fails so metric recording never aborts an attempt."""
    from retopo_agent.blender.shape import evaluate_shape_match_blender
    from retopo_agent.geometry.shape_eval import DECIMATION_SHAPE_THRESHOLDS

    try:
        rep = evaluate_shape_match_blender(
            proxy_obj, low_obj, thresholds=DECIMATION_SHAPE_THRESHOLDS
        )
    except Exception as exc:  # noqa: BLE001 - shape eval is informational here
        return {"error": str(exc)}
    return {
        "mean": round(rep.surface_distance_mean, 6),
        "max": round(rep.surface_distance_max, 6),
        "normal_dev": round(rep.normal_deviation_mean_deg, 3),
    }


def _mesh_topology(obj) -> dict:
    """Non-manifold-edge and connected-component counts for a bpy mesh."""
    from retopo_agent.blender.proxy import _bmesh_topology

    topo = _bmesh_topology(obj)
    return {
        "non_manifold_edges": topo["non_manifold_edges"],
        "boundary_edges": topo["boundary_edges"],
        "components": len(topo["component_sizes"]),
    }


# --- Phase A3 — polygon cleanup (mixed tri/quad look) (plan §6) -------------


class CleanupAssertionError(AssertionError):
    """Raised when A3's post-cleanup hard asserts fail (plan §6.2) so a broken mesh
    (n-gons, non-manifold, exploded component count, out-of-band) never advances to
    UV/export. Carries the :func:`cleanup_asserts` record for the report."""

    def __init__(self, message: str, asserts: dict):
        super().__init__(message)
        self.asserts = asserts


def cleanup_to_mixed_poly(
    low_obj,
    *,
    target_face_count: int,
    face_threshold_deg: float = 15.0,
    shape_threshold_deg: float = 15.0,
    smooth_angle_deg: float = 35.0,
    band_tol: float = 0.10,
    component_bound: int = 1,
) -> dict:
    """Phase A3 — turn the fully-triangulated A2 collapse into the requested natural
    tri/quad mix and lock in the hard invariants (plan §6).

    Steps, in order:

    1. **Tris→Quads** (``mesh.tris_convert_to_quads``) with conservative angle
       limits (``face_threshold_deg`` / ``shape_threshold_deg`` ≈ 10–25°): coplanar
       triangle pairs merge into quads exactly where quads fit, leaving triangles on
       detail — the reference's 51-quad pattern. We do NOT chase a quad-ratio number.
    2. **Degenerate cleanup** (``mesh.dissolve_degenerate``) to drop zero-area slivers
       the merge can expose.
    3. **Hard asserts** (:func:`cleanup_asserts`): 0 n-gons, 0 non-manifold, components
       ≤ ``component_bound``, face count still within T_goal ±``band_tol``. Raises
       :class:`CleanupAssertionError` on any failure.
    4. **Normals**: ``shade_smooth`` + smooth-by-angle ≈ ``smooth_angle_deg``.

    Returns a report dict (face breakdown before/after, topology, asserts).
    """
    import math

    import bpy
    from retopo_agent.blender.retopo import _make_only_active

    before = _mesh_face_breakdown(low_obj)
    _make_only_active(low_obj)

    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.mesh.select_all(action="SELECT")
        # tris_convert_to_quads angle args are radians; conservative limits keep the
        # mix organic (only near-coplanar pairs merge).
        bpy.ops.mesh.tris_convert_to_quads(
            face_threshold=math.radians(face_threshold_deg),
            shape_threshold=math.radians(shape_threshold_deg),
        )
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.dissolve_degenerate()
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")

    after = _mesh_face_breakdown(low_obj)
    topo = _mesh_topology(low_obj)
    asserts = cleanup_asserts(
        after, topo, target_face_count=target_face_count,
        band_tol=band_tol, component_bound=component_bound,
    )

    if not asserts["all_ok"]:
        raise CleanupAssertionError(
            f"A3 cleanup asserts failed: ngons={after['ngons']} "
            f"non_manifold={topo['non_manifold_edges']} components={topo['components']} "
            f"faces={after['faces']} (target {target_face_count}); {asserts}",
            asserts,
        )

    _shade_smooth(low_obj, smooth_angle_deg)

    return {
        "before": before,
        "after": after,
        "topology": topo,
        "asserts": asserts,
        "quad_threshold_deg": {"face": face_threshold_deg, "shape": shape_threshold_deg},
        "smooth_angle_deg": smooth_angle_deg,
        "quads_gained": after["quads"] - before["quads"],
    }


def _shade_smooth(obj, smooth_angle_deg: float) -> None:
    """``shade_smooth`` + smooth-by-angle (plan §6.3). Uses the auto-smooth angle so
    creases sharper than ``smooth_angle_deg`` stay faceted. Best-effort: the operator
    name varies across Blender 4/5, so a failure is non-fatal."""
    import math

    import bpy
    from retopo_agent.blender.retopo import _make_only_active

    _make_only_active(obj)
    try:
        bpy.ops.object.shade_smooth()
    except RuntimeError:
        return
    angle = math.radians(smooth_angle_deg)
    # Blender 4.1+ replaced mesh.use_auto_smooth with the smooth-by-angle operator.
    try:
        bpy.ops.object.shade_smooth_by_angle(angle=angle)
    except (AttributeError, RuntimeError, TypeError):
        mesh = obj.data
        if hasattr(mesh, "use_auto_smooth"):
            mesh.use_auto_smooth = True
            mesh.auto_smooth_angle = angle
