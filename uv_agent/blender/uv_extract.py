"""Read an existing Blender UV layer into a :class:`UVMap` (MVP 1 plan §3, Session A).

MVP 1 reviews UVs that already exist on the working model; it never creates them.
This adapter:

- lists an object's UV layers with the metadata the UI needs
  (:func:`list_uv_layers`),
- reads the active (or a named) UV layer per-loop into a :class:`UVMap` aligned
  with :func:`uv_agent.blender.extract.extract_mesh_graph`
  (:func:`extract_mesh_graph_with_uv`),
- summarizes an object for the ``inspect_uv_layers`` command
  (:func:`object_uv_summary`),

and returns a typed "no UV" result instead of raising when an object has no UV
layer (plan §13 / Session A acceptance "no-UV object는 exception 대신 typed
result를 반환한다").

Only runs inside Blender (``bmesh`` imported lazily). Reads only — it never marks
seams, writes UVs, or saves the file.
"""

from __future__ import annotations

from uv_agent.blender.extract import extract_mesh_graph
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

_EMPTY_UV_TOL = 1e-9


def _layer_is_empty(uv_layer) -> bool:
    """A UV layer is "empty" when every loop UV is at the origin (never authored)."""
    data = uv_layer.data
    n = len(data)
    if n == 0:
        return True
    try:
        import numpy as np

        flat = np.empty(n * 2, dtype="f4")
        data.foreach_get("uv", flat)
        return bool(np.abs(flat).max() < _EMPTY_UV_TOL)
    except (ImportError, AttributeError):  # pragma: no cover - defensive
        return all(abs(d.uv[0]) < _EMPTY_UV_TOL and abs(d.uv[1]) < _EMPTY_UV_TOL for d in data)


def list_uv_layers(obj) -> list[dict]:
    """Per-UV-layer metadata for the inspect/select UI (plan §5.1 ``uv_layers``)."""
    mesh = obj.data
    active = mesh.uv_layers.active
    out: list[dict] = []
    for uv in mesh.uv_layers:
        out.append({
            "name": uv.name,
            "active": bool(active is not None and uv.name == active.name),
            "loop_count": len(uv.data),
            "empty": _layer_is_empty(uv),
        })
    return out


def active_uv_layer_name(obj) -> str | None:
    active = obj.data.uv_layers.active
    return active.name if active is not None else None


def resolve_uv_layer_name(obj, layer_name: str | None) -> str | None:
    """The UV layer to review: the requested one if it exists, else the active one.

    Returns ``None`` when the object has no UV layer or the requested name is
    missing and there is no active layer (the caller turns this into ``no_uv``).
    """
    layers = obj.data.uv_layers
    if len(layers) == 0:
        return None
    if layer_name and layer_name in layers:
        return layer_name
    return active_uv_layer_name(obj)


def read_uv_layer(obj, layer_name: str | None = None) -> tuple[UVMap | None, str | None]:
    """Read a UV layer into a :class:`UVMap`, indexed by bmesh loop order.

    The loop traversal (``bm.faces`` then ``f.loops``) matches
    :func:`extract_mesh_graph` exactly, so the returned UVMap's loop indices align
    with the mesh graph's loops. Returns ``(None, None)`` when the object has no UV
    layer, or the resolved layer is **empty** (every UV at the origin — e.g. the
    placeholder layer Blender's OBJ importer adds when the file has no ``vt``).
    An empty layer is "nothing to review", so the worker reports ``no_uv`` — this
    matches :func:`object_uv_summary`'s ``has_uv`` (non-empty) check.
    """
    import bmesh  # lazy: only available inside Blender

    resolved = resolve_uv_layer_name(obj, layer_name)
    if resolved is None:
        return None, None

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        uv_lay = bm.loops.layers.uv.get(resolved)
        if uv_lay is None:  # pragma: no cover - resolved name always exists
            uv_lay = bm.loops.layers.uv.active
        if uv_lay is None:
            return None, None

        coords: list[tuple[float, float]] = []
        for f in bm.faces:
            for loop in f.loops:
                uv = loop[uv_lay].uv
                coords.append((float(uv[0]), float(uv[1])))

        uvmap = UVMap(len(coords))
        for i, (u, v) in enumerate(coords):
            uvmap.set(i, u, v)
        if len(uvmap.uv) == 0 or float(abs(uvmap.uv).max()) < _EMPTY_UV_TOL:
            return None, None  # empty placeholder layer -> treat as no UV
        return uvmap, resolved
    finally:
        bm.free()


def extract_mesh_graph_with_uv(
    obj, layer_name: str | None = None
) -> tuple[MeshGraph, UVMap | None, str | None]:
    """Extract the mesh graph and read its UV layer in one call (plan §3 adapter).

    Returns ``(mesh, uvmap, resolved_layer_name)``. ``uvmap`` is ``None`` and
    ``resolved_layer_name`` is ``None`` for a no-UV object (typed result, never an
    exception). The UVMap's loop count is asserted to equal the mesh loop count so
    a Blender-side traversal mismatch fails loudly rather than silently misaligning.
    """
    mesh = extract_mesh_graph(obj)
    uvmap, resolved = read_uv_layer(obj, layer_name)
    if uvmap is not None and len(uvmap.uv) != len(mesh.loops):
        raise RuntimeError(
            f"UV loop count {len(uvmap.uv)} != mesh loop count {len(mesh.loops)} "
            f"for object {obj.name!r}, layer {resolved!r}"
        )
    return mesh, uvmap, resolved


def extract_uv_boundary_edges(
    obj, layer_name: str | None = None
) -> tuple[list[int], dict]:
    """Resolve a UV layer and return its island-boundary mesh-edge ids + a report
    (Electron MVP 3 UV-boundary-fallback revision plan §4.3).

    Reuses :func:`extract_mesh_graph_with_uv` (the MVP 1 adapter) and the pure
    :func:`uv_agent.geometry.uv_boundary.extract_uv_boundary_seams` so the
    Generate + Optimize worker can derive a seam spec from an *existing* UV layer
    as a library call — no subprocess into the MVP 2 seam-editor worker (revision
    plan §4.3 "worker 간 subprocess 호출보다 library helper로 분리해 재사용").
    Blender-only (reads UVs via ``bmesh``).

    Returns ``(edge_ids, report)``. ``edge_ids`` are mesh edge ids aligned with
    :func:`uv_agent.blender.extract.extract_mesh_graph` (the same ids
    ``UserSeamSpec`` is validated against). When the object has no usable
    (non-empty) UV layer, ``edge_ids`` is empty and ``report["uv_layer_missing"]``
    is ``True`` — the caller turns that into ``needs_input`` (revision plan §1
    case 3). Reads only; never marks seams, edits UVs, or saves the file.
    """
    from uv_agent.geometry.uv_boundary import extract_uv_boundary_seams

    mesh, uvmap, resolved = extract_mesh_graph_with_uv(obj, layer_name)
    if uvmap is None or resolved is None:
        return [], {
            "uv_layer": None,
            "requested_uv_layer": layer_name,
            "uv_layer_missing": True,
            "boundary_edge_count": 0,
            "mesh_boundary_edges": [],
            "non_manifold_edges": [],
            "ambiguous_edges": [],
        }
    boundary = extract_uv_boundary_seams(mesh, uvmap)
    report = boundary.report()
    report["uv_layer"] = resolved
    report["requested_uv_layer"] = layer_name
    return list(boundary.seam_edges), report


def object_uv_summary(obj) -> dict:
    """Summarize one object for the ``inspect_uv_layers`` command (plan §5.1)."""
    mesh = obj.data
    layers = list_uv_layers(obj)
    return {
        "name": obj.name,
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "faces": len(mesh.polygons),
        "materials": [m.name for m in mesh.materials if m is not None],
        "uv_layers": layers,
        "active_uv_layer": active_uv_layer_name(obj),
        "has_uv": len(layers) > 0 and any(not lyr["empty"] for lyr in layers),
    }
