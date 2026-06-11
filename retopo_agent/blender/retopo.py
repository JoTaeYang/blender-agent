"""Blender low-poly generation + projection (retopology plan §10 Phase 1).

Implements the §15.5 default pipeline for a single high-poly object:

    duplicate -> QuadriFlow Remesh -> Shrinkwrap to original -> result object

with the §15.5 fallback ladder *and* the §15.7 target-face-count control loop so
the result actually lands near the requested face count (the missing piece that
produced ``target 10000 -> actual 2774`` on the anchor model):

    QuadriFlow Remesh   (default, quad-oriented; target_faces retried to converge)
      -> Voxel Remesh   (fallback; voxel size binary-searched to hit the target)
      -> cluster decimate (Blender-free, deterministic last resort)

Each remesh attempt starts from a fresh copy of the high-poly so that successive
attempts at different settings are independent (voxel/QuadriFlow remeshes are
destructive). The actual face count is measured after every attempt and fed back
into the search loops in :mod:`retopo_agent.geometry.target_search`, which are
unit-tested offline.

Everything here only runs inside Blender (``bpy`` imported lazily). QuadriFlow is
skipped for very large inputs, where it is impractically slow / prone to failing
silently -- the spec lists Voxel Remesh as the cleanup path for such meshes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from retopo_agent.geometry.target_search import (
    quality_band,
    search_quadriflow_target,
    search_voxel_size,
    target_error_ratio,
)
from uv_agent.blender.extract import extract_mesh_graph
from uv_agent.geometry.mesh_graph import MeshGraph

RESULTS_COLLECTION = "AI_Retopo_Results"

# QuadriFlow is impractically slow and tends to fail silently on multi-million-
# face inputs; above this we go straight to the voxel-remesh control loop.
QUADRIFLOW_MAX_INPUT_FACES = 1_500_000


@dataclass
class GenerateResult:
    obj: object  # the new low-poly bpy object
    method: str  # quadriflow_remesh | voxel_remesh | cluster_decimate
    source_face_count: int
    target_face_count: int
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


def lowpoly_name(source_name: str, target_face_count: int) -> str:
    """``{original_name}_LOW_{target_face_count}`` (plan §15.4)."""
    return f"{source_name}_LOW_{target_face_count}"


def generate_lowpoly_object(
    obj,
    target_face_count: int,
    *,
    apply_shrinkwrap: bool = True,
    preserve_sharp: bool = True,
    use_quadriflow: bool = True,
    preserve_features: bool = False,
    feature_angle: float = 30.0,
    voxel_adaptivity: float = 0.0,
    max_voxel_iter: int = 6,
    max_quadriflow_iter: int = 3,
    quadriflow_max_input_faces: int = QUADRIFLOW_MAX_INPUT_FACES,
) -> GenerateResult:
    """Duplicate ``obj`` and reduce it toward ``target_face_count`` (plan §10/§15.7).

    Tries each method in preference order, measuring the actual face count and
    retrying its control parameter, and stops as soon as a method reaches the
    ``accepted`` band. The new object is placed in ``AI_Retopo_Results`` and
    named per §15.4. ``GenerateResult`` exposes the achieved ``band`` / error.

    Phase 5 feature preservation (``preserve_features``): before each QuadriFlow
    attempt, edges above ``feature_angle`` are marked sharp so QuadriFlow keeps
    them; the voxel path uses ``voxel_adaptivity`` (> 0 reduces face density in
    flat regions while keeping it on curved/feature areas).
    """
    import bpy  # noqa: F401

    notes: list[str] = []
    source_faces = len(obj.data.polygons)

    dup = obj.copy()
    dup.data = obj.data.copy()
    dup.name = lowpoly_name(obj.name, target_face_count)
    dup.data.name = dup.name
    _link_to_results_collection(dup)
    _make_only_active(dup)

    method = "none"

    # -- Stage 1: QuadriFlow with target_faces retry ----------------------
    if use_quadriflow and source_faces <= quadriflow_max_input_faces and _quadriflow_available():
        def qf_remesh(requested: int) -> int:
            _reset_from_source(dup, obj)
            if preserve_features:
                from retopo_agent.blender.features import mark_sharp_edges_by_angle

                marked = mark_sharp_edges_by_angle(dup, feature_angle)
                notes.append(f"marked {marked} sharp edges (>= {feature_angle} deg) for QuadriFlow")
            return _run_quadriflow(dup, requested, preserve_sharp or preserve_features, notes)

        qf = search_quadriflow_target(qf_remesh, target_face_count, max_iter=max_quadriflow_iter)
        if qf.face_count > 0:
            qf_remesh(int(qf.value))  # re-apply the best request so dup holds it
            method = "quadriflow_remesh"
            notes.append(
                f"quadriflow_remesh: target_faces={int(qf.value)} -> {qf.face_count} faces "
                f"({qf.iterations} iter, band={qf.band})"
            )
    elif use_quadriflow and source_faces > quadriflow_max_input_faces:
        notes.append(
            f"quadriflow skipped: {source_faces} faces exceeds {quadriflow_max_input_faces} (using voxel remesh)"
        )

    # -- Stage 2: Voxel remesh with voxel-size binary search --------------
    if quality_band(len(dup.data.polygons), target_face_count) != "accepted" and _voxel_available():
        diag = _source_diagonal(obj)
        initial = max(diag / max((target_face_count / 2.0) ** 0.5, 1.0), diag / 256.0)

        def voxel_measure(voxel: float) -> int:
            _reset_from_source(dup, obj)
            return _run_voxel_remesh(dup, voxel, notes, adaptivity=voxel_adaptivity)

        vr = search_voxel_size(
            voxel_measure,
            target_face_count,
            initial=initial,
            min_voxel=diag / 4096.0,
            max_voxel=diag,
            max_iter=max_voxel_iter,
        )
        if vr.face_count > 0:
            voxel_measure(vr.value)  # re-apply best voxel size so dup holds it
            method = "voxel_remesh"
            notes.append(
                f"voxel_remesh: voxel={vr.value:.4g} -> {vr.face_count} faces "
                f"({vr.iterations} iter, band={vr.band})"
            )

    # -- Stage 3: deterministic Blender-free fallback ---------------------
    if method == "none" or quality_band(len(dup.data.polygons), target_face_count) == "failed":
        _reset_from_source(dup, obj)
        _cluster_decimate_object(dup, target_face_count, notes)
        method = "cluster_decimate"

    if apply_shrinkwrap:
        _apply_shrinkwrap(dup, obj, notes)

    result = GenerateResult(dup, method, source_faces, target_face_count, notes)
    notes.append(
        f"result: {result.actual_face_count} faces, target {target_face_count}, "
        f"error={result.target_error_ratio:.4f}, band={result.band}"
    )
    return result


# -- remesh operators (single attempt each) --------------------------------


def _quadriflow_available() -> bool:
    import bpy

    return hasattr(bpy.ops.object, "quadriflow_remesh")


def _voxel_available() -> bool:
    import bpy

    return hasattr(bpy.ops.object, "voxel_remesh")


def _run_quadriflow(dup, requested: int, preserve_sharp: bool, notes: list[str]) -> int:
    """One QuadriFlow attempt. Returns the resulting face count, or ``0`` if the
    operator failed or produced no usable change (improved failure detection)."""
    import bpy

    before = len(dup.data.polygons)
    try:
        bpy.ops.object.quadriflow_remesh(
            target_faces=max(8, int(requested)),
            mode="FACES",
            use_mesh_symmetry=False,
            use_preserve_sharp=bool(preserve_sharp),
            use_preserve_boundary=True,
        )
    except (RuntimeError, TypeError) as exc:
        notes.append(f"quadriflow_remesh failed (req={requested}): {exc}")
        return 0
    after = len(dup.data.polygons)
    if after == 0 or after == before:
        notes.append(f"quadriflow_remesh produced no change (req={requested}, faces={after})")
        return 0
    return after


def _run_voxel_remesh(dup, voxel: float, notes: list[str], *, adaptivity: float = 0.0) -> int:
    """One voxel-remesh attempt at ``voxel`` size. ``adaptivity`` > 0 reduces face
    density in flat regions (Phase 5). Returns the face count, or ``0`` on failure."""
    import bpy

    try:
        dup.data.remesh_voxel_size = float(voxel)
        dup.data.remesh_voxel_adaptivity = max(0.0, float(adaptivity))
        bpy.ops.object.voxel_remesh()
    except (RuntimeError, AttributeError) as exc:
        notes.append(f"voxel_remesh failed (voxel={voxel:.4g}): {exc}")
        return 0
    return len(dup.data.polygons)


def _cluster_decimate_object(dup, target_face_count: int, notes: list[str]) -> None:
    """Deterministic Blender-free fallback: rebuild ``dup``'s mesh from a
    cluster-decimated :class:`MeshGraph` (see :mod:`retopo_agent.geometry.decimate`)."""
    from retopo_agent.geometry.decimate import decimate_to_target

    graph = extract_mesh_graph(dup)
    result = decimate_to_target(graph, target_face_count)
    _replace_mesh_from_graph(dup, result.low_mesh)
    notes.append(
        f"cluster_decimate: grid={result.grid}, {result.source_face_count} -> "
        f"{result.actual_face_count} faces (band={quality_band(result.actual_face_count, target_face_count)})"
    )


def _apply_shrinkwrap(dup, source_obj, notes: list[str]) -> None:
    """Project the low-poly back onto the high-poly surface (plan §6.5 / Ticket 4)."""
    import bpy

    try:
        mod = dup.modifiers.new(name="AI_Retopo_Shrinkwrap", type="SHRINKWRAP")
        mod.target = source_obj
        mod.wrap_method = "NEAREST_SURFACEPOINT"
        mod.offset = 0.0
        _make_only_active(dup)
        bpy.ops.object.modifier_apply(modifier=mod.name)
        notes.append("shrinkwrap applied")
    except (RuntimeError, AttributeError) as exc:
        notes.append(f"shrinkwrap failed: {exc}")


# -- helpers ---------------------------------------------------------------


def _source_diagonal(obj) -> float:
    d = obj.dimensions
    return max(float((d.x ** 2 + d.y ** 2 + d.z ** 2) ** 0.5), 1e-6)


def _link_to_results_collection(obj) -> None:
    import bpy

    coll = bpy.data.collections.get(RESULTS_COLLECTION)
    if coll is None:
        coll = bpy.data.collections.new(RESULTS_COLLECTION)
        bpy.context.scene.collection.children.link(coll)
    coll.objects.link(obj)


def _make_only_active(obj) -> None:
    import bpy

    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _reset_from_source(dup, source_obj) -> None:
    """Replace ``dup``'s mesh with a fresh copy of the high-poly source, so the
    next remesh attempt is independent of the previous one (remeshes are
    destructive)."""
    import bpy

    old = dup.data
    dup.data = source_obj.data.copy()
    dup.data.name = dup.name
    if old.users == 0:
        bpy.data.meshes.remove(old)
    _make_only_active(dup)


def _replace_mesh_from_graph(obj, graph: MeshGraph) -> None:
    """Swap an object's mesh data for one built from a :class:`MeshGraph`."""
    import bpy

    new_mesh = bpy.data.meshes.new(obj.data.name)
    verts = [tuple(v.co) for v in graph.vertices]
    faces = [list(f.vertex_ids) for f in graph.faces]
    new_mesh.from_pydata(verts, [], faces)
    new_mesh.update()
    old = obj.data
    obj.data = new_mesh
    if old.users == 0:
        bpy.data.meshes.remove(old)
