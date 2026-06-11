"""Blender Decimate-Collapse generator (decimation plan §6.3, Phase D1).

The Decimation Optimize mode is a sibling of the quad-retopo generator in
:mod:`retopo_agent.blender.retopo`. Its goal is the opposite tradeoff: not clean
quad edge flow, but aggressive triangle-based polygon reduction that preserves
the silhouette -- ZBrush Decimation Master, not manual retopology (plan §2).

Phase D1 scope (plan §7 "Phase D1. Basic Decimation Mode", Tickets D1/D2):

    duplicate -> Decimate (COLLAPSE) modifier -> ratio search -> result object

The Decimate modifier's ``ratio`` is only loosely tied to the final face count
(it keeps ~``ratio`` of the source faces), so -- exactly like the QuadriFlow /
voxel paths -- we **close the loop on the target face count**: measure the actual
result after each attempt and rescale the ratio by ``target / actual`` until it
lands in the acceptance band. The control loop is
:func:`retopo_agent.geometry.target_search.search_decimate_ratio` (pure,
unit-tested offline); this module wires it to the real modifier.

Each attempt restarts from a fresh copy of the high-poly because applying a
modifier is destructive. Triangles are allowed (Collapse triangulates); n-gons
are never produced. Everything here only runs inside Blender (``bpy`` lazy).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from retopo_agent.blender.retopo import (
    _link_to_results_collection,
    _make_only_active,
    _reset_from_source,
)
from retopo_agent.geometry.target_search import (
    quality_band,
    search_decimate_ratio,
    target_error_ratio,
)


@dataclass
class DecimateResult:
    obj: object  # the new low-poly bpy object
    method: str  # decimate_collapse
    source_face_count: int
    target_face_count: int
    ratio: float  # the Decimate (Collapse) ratio that produced the result
    preserve_features: bool = False
    feature_vertex_count: int = 0
    notes: list[str] = field(default_factory=list)
    # DM4 importance-map feature protection (plan §7).
    preserve_features_strength: float = 1.0
    importance_weighted: bool = False
    # DM1 plateau-detection metadata carried from the ratio search (plan §4).
    stopped_reason: str = ""
    plateau_face_count: int | None = None
    plateau_ratio: float | None = None
    hit_min_ratio: bool = False
    search_iterations: int = 0
    search_history: list[tuple[float, int]] = field(default_factory=list)

    @property
    def actual_face_count(self) -> int:
        return len(self.obj.data.polygons)

    @property
    def target_error_ratio(self) -> float:
        return target_error_ratio(self.actual_face_count, self.target_face_count)

    @property
    def band(self) -> str:
        return quality_band(self.actual_face_count, self.target_face_count)


def decimated_name(source_name: str, target_face_count: int) -> str:
    """``{original_name}_DECIMATED_{target_face_count}`` (cf. §15.4 naming)."""
    return f"{source_name}_DECIMATED_{target_face_count}"


FEATURE_VERTEX_GROUP = "AI_Decimate_Features"


def generate_decimated_object(
    obj,
    target_face_count: int,
    *,
    triangulate: bool = True,
    preserve_features: bool = False,
    feature_angle: float = 30.0,
    preserve_features_strength: float = 1.0,
    importance_weights=None,
    max_iter: int = 8,
) -> DecimateResult:
    """Duplicate ``obj`` and reduce it toward ``target_face_count`` with the
    Decimate (Collapse) modifier (plan §6.3 / Phases D1, D3, DM4).

    The duplicate is placed in ``AI_Retopo_Results`` and named per
    :func:`decimated_name`. The collapse ``ratio`` is searched so the actual face
    count lands near the target; ``DecimateResult`` exposes the achieved
    ``band`` / ``target_error_ratio``. ``triangulate`` keeps the Collapse output
    fully triangulated (its default), which is acceptable here -- triangles are
    allowed in this mode, n-gons are not (plan §3).

    Feature preservation drives the Collapse modifier's ``vertex_group`` weighting
    so feature regions are decimated *less* and flat areas *more*:

    - ``importance_weights`` (DM4, plan §7): a per-source-vertex array/dict of
      *graded* importance in ``[0, 1]`` (from the DM4 importance map). When given it
      is used directly as the vertex-group weights -- curvature / seams / material
      borders are protected proportionally, not just hard edges.
    - else ``preserve_features`` (D3): the binary hard-edge (dihedral >=
      ``feature_angle``) + open-boundary vertex set, each weighted 1.0.

    ``preserve_features_strength`` sets the modifier's ``vertex_group_factor`` --
    the global strength of that protection (plan §7 "feature 보호 강도"). The weights
    are computed once from the source and re-applied on each ratio-search attempt
    (a fresh source copy preserves vertex ordering).
    """
    import bpy  # noqa: F401

    notes: list[str] = []
    source_faces = len(obj.data.polygons)

    dup = obj.copy()
    dup.data = obj.data.copy()
    dup.name = decimated_name(obj.name, target_face_count)
    dup.data.name = dup.name
    _link_to_results_collection(dup)
    _make_only_active(dup)

    # Build the per-vertex protection weights (vid -> weight in (0, 1]).
    weights: dict[int, float] = {}
    importance_weighted = False
    if importance_weights is not None:
        weights = _weights_from_importance(importance_weights)
        importance_weighted = True
        notes.append(
            f"importance-map preservation on: {len(weights)} weighted verts "
            f"(strength={preserve_features_strength})"
        )
    elif preserve_features:
        feature_vids = _feature_vertex_indices(obj, feature_angle)
        weights = {vid: 1.0 for vid in feature_vids}
        notes.append(
            f"feature preservation on: {len(weights)} feature verts (>= {feature_angle} deg, "
            f"strength={preserve_features_strength})"
        )

    def collapse(ratio: float) -> int:
        return _run_decimate_collapse(
            dup, obj, ratio, triangulate, weights, preserve_features_strength, notes
        )

    search = search_decimate_ratio(collapse, target_face_count, source_faces, max_iter=max_iter)
    if search.face_count > 0:
        collapse(search.value)  # re-apply the best ratio so dup holds it
        notes.append(
            f"decimate_collapse: ratio={search.value:.4g} -> {search.face_count} faces "
            f"({search.iterations} iter, band={search.band}, stopped={search.stopped_reason})"
        )
    else:
        notes.append("decimate_collapse produced no faces; result is the source copy")

    # DM1: surface *why* the search stopped, so a plateaued / floored result is
    # explained rather than reported as a bare ``failed`` (plan §4).
    if search.is_plateau:
        notes.append(
            f"plateau detected: Collapse floored at {search.plateau_face_count} faces "
            f"(ratio={search.plateau_ratio:.4g}); target {target_face_count} unreachable by ratio alone"
        )
    elif search.hit_min_ratio and search.band != "accepted":
        notes.append(f"ratio search hit min_ratio with target {target_face_count} unmet")

    result = DecimateResult(
        dup, "decimate_collapse", source_faces, target_face_count, search.value,
        preserve_features=preserve_features or importance_weighted,
        feature_vertex_count=len(weights), notes=notes,
        preserve_features_strength=preserve_features_strength,
        importance_weighted=importance_weighted,
        stopped_reason=search.stopped_reason,
        plateau_face_count=search.plateau_face_count,
        plateau_ratio=search.plateau_ratio,
        hit_min_ratio=search.hit_min_ratio,
        search_iterations=search.iterations,
        search_history=search.history,
    )
    notes.append(
        f"result: {result.actual_face_count} faces, target {target_face_count}, "
        f"error={result.target_error_ratio:.4f}, band={result.band}"
    )
    return result


def _run_decimate_collapse(dup, source_obj, ratio: float, triangulate: bool, weights, strength: float, notes: list[str]) -> int:
    """One Decimate-Collapse attempt at ``ratio``. Restarts ``dup`` from a fresh
    copy of the high-poly first (applying a modifier is destructive), rebuilds the
    importance/feature vertex group (lost with the old mesh) from the ``weights``
    map (``vid -> weight``) when given, applies the modifier with
    ``vertex_group_factor = strength``, and returns the resulting face count, or
    ``0`` on failure."""
    import bpy

    _reset_from_source(dup, source_obj)
    vgroup_name = _apply_importance_vertex_group(dup, weights) if weights else None
    try:
        mod = dup.modifiers.new(name="AI_Decimate", type="DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = float(min(1.0, max(0.0, ratio)))
        mod.use_collapse_triangulate = bool(triangulate)
        if vgroup_name:
            mod.vertex_group = vgroup_name
            mod.invert_vertex_group = False  # weighted (feature) verts decimated less
            mod.vertex_group_factor = float(max(0.0, strength))  # DM4 protection strength
        _make_only_active(dup)
        bpy.ops.object.modifier_apply(modifier=mod.name)
    except (RuntimeError, AttributeError) as exc:
        notes.append(f"decimate_collapse failed (ratio={ratio:.4g}): {exc}")
        return 0
    return len(dup.data.polygons)


def _feature_vertex_indices(obj, feature_angle: float) -> list[int]:
    """Indices of vertices that touch a hard edge (dihedral >= ``feature_angle``)
    or an open boundary -- the features to protect (plan §6.1). Computed once from
    the source; vertex ordering is preserved by ``data.copy()`` so the indices stay
    valid across ratio-search resets."""
    import bmesh
    import math

    threshold = math.radians(feature_angle)
    bm = bmesh.new()
    feature: set[int] = set()
    try:
        bm.from_mesh(obj.data)
        for e in bm.edges:
            if len(e.link_faces) == 2:
                hard = e.calc_face_angle(0.0) >= threshold
            else:
                hard = True  # boundary / non-manifold -> silhouette feature
            if hard:
                feature.add(e.verts[0].index)
                feature.add(e.verts[1].index)
    finally:
        bm.free()
    return sorted(feature)


def _weights_from_importance(importance_weights) -> dict[int, float]:
    """Normalize a per-vertex importance array/dict into a ``{vid: weight}`` map of
    the non-zero weights (DM4 graded vertex group)."""
    if isinstance(importance_weights, dict):
        items = importance_weights.items()
    else:
        items = enumerate(importance_weights)
    out: dict[int, float] = {}
    for vid, w in items:
        w = float(w)
        if w > 0.0:
            out[int(vid)] = min(1.0, w)
    return out


def _apply_importance_vertex_group(dup, weights) -> str | None:
    """(Re)create the ``AI_Decimate_Features`` vertex group on ``dup`` and assign
    the graded importance ``weights`` (``vid -> weight in (0, 1]``). Vertices are
    batched by quantized weight so a large graded map is added in a few calls.
    Returns the group name, or ``None`` if there are no weighted vertices."""
    if not weights:
        return None
    existing = dup.vertex_groups.get(FEATURE_VERTEX_GROUP)
    if existing is not None:
        dup.vertex_groups.remove(existing)
    vg = dup.vertex_groups.new(name=FEATURE_VERTEX_GROUP)
    buckets: dict[float, list[int]] = {}
    for vid, w in weights.items():
        buckets.setdefault(round(float(w), 3), []).append(int(vid))
    for weight, vids in buckets.items():
        vg.add(vids, float(weight), "REPLACE")
    return vg.name
