"""Blender adapter for the DM2 pre-process diagnosis (decimation plan §5).

Extracts a :class:`~uv_agent.geometry.mesh_graph.MeshGraph` from a Blender object
and runs the pure-geometry :func:`retopo_agent.geometry.diagnosis.diagnose_topology`
on it, so the diagnosis logic is shared with the offline unit tests.

A full Python mesh graph of a multi-million-face high-poly is too heavy to build,
so this is meant for the *decimated result* (e.g. the anchor's 8008-face Collapse
plateau), which is small and is exactly the mesh whose 25-component / 20-tiny
structure the plan's §5 example reports. ``max_graph_faces`` guards against an
accidental call on an over-large mesh: it returns ``None`` with a note rather than
stalling. Only runs inside Blender (``bpy`` data access is lazy via the extractor).
"""

from __future__ import annotations


def diagnose_decimation_blender(obj, *, max_graph_faces: int = 2_000_000, **kwargs):
    """Diagnose ``obj``'s topology for decimation pre-process (plan §5).

    Returns a :class:`~retopo_agent.geometry.diagnosis.DiagnosisReport`, or
    ``None`` if the mesh exceeds ``max_graph_faces`` (too large for a pure-Python
    graph). ``kwargs`` are forwarded to ``diagnose_topology`` (relative thresholds).
    """
    from retopo_agent.geometry.diagnosis import diagnose_topology
    from uv_agent.blender.extract import extract_mesh_graph

    face_count = len(obj.data.polygons)
    if face_count > max_graph_faces:
        return None
    mesh = extract_mesh_graph(obj)
    return diagnose_topology(mesh, **kwargs)
