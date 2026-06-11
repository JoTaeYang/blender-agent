"""Blender adapter for the DM4 importance map (decimation plan §7).

Extracts a :class:`~uv_agent.geometry.mesh_graph.MeshGraph` from a Blender object
and runs the pure-geometry :func:`retopo_agent.geometry.importance.compute_importance_map`
on it, so the importance logic is shared with the offline unit tests. The result
both feeds the ``importance_map.json`` report (stats + sources) and -- via
:func:`importance_vertex_weights` -- the Decimate Collapse vertex group, decimating
flat areas more and feature regions less (plan §7 short-term modifier connection).

A full Python graph of a multi-million-face high-poly is too heavy, so
``max_graph_faces`` guards the call: it returns ``None`` rather than stalling, and
the decimation falls back to the binary hard-edge / boundary feature group. Only
runs inside Blender (graph extraction touches ``bpy`` data lazily).
"""

from __future__ import annotations


def compute_importance_map_blender(obj, *, max_graph_faces: int = 2_000_000, **kwargs):
    """Compute the importance map of ``obj`` (plan §7).

    Returns a :class:`~retopo_agent.geometry.importance.ImportanceMap`, or ``None``
    if the mesh exceeds ``max_graph_faces``. ``kwargs`` forward to
    ``compute_importance_map`` (``angle_threshold``, ``weights``, ``enabled_sources``...).
    """
    from retopo_agent.geometry.importance import compute_importance_map
    from uv_agent.blender.extract import extract_mesh_graph

    if len(obj.data.polygons) > max_graph_faces:
        return None
    return compute_importance_map(extract_mesh_graph(obj), **kwargs)


def importance_vertex_weights(obj, *, strength: float = 1.0, max_graph_faces: int = 2_000_000, **kwargs):
    """Per-vertex Decimate-Collapse weights for ``obj`` from its importance map.

    Returns a ``{vertex_index: weight}`` dict (only the non-zero weights), or
    ``None`` if the importance map could not be computed (mesh too large). The
    weights are :func:`importance_to_vertex_weights` of the map's vertex importance,
    sharpened by ``strength`` (``preserve_features_strength``)."""
    from retopo_agent.geometry.importance import importance_to_vertex_weights

    imap = compute_importance_map_blender(obj, max_graph_faces=max_graph_faces, **kwargs)
    if imap is None:
        return None
    weights = importance_to_vertex_weights(imap.vertex_importance, strength=strength)
    return {vid: float(w) for vid, w in enumerate(weights) if w > 0.0}
