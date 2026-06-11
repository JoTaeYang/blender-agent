"""AI Retopology Agent (retopology plan §1).

A sibling engine to :mod:`uv_agent`: it takes a high-poly mesh and produces a
lower-poly candidate close to a target face count, preserving the overall shape.

Phase 1 (retopology plan §10 "Blender Retopo Prototype") lives here:

- a deterministic, Blender-free low-poly generator (vertex-clustering decimation)
  that reduces a :class:`~uv_agent.geometry.mesh_graph.MeshGraph` toward a target
  face count -- see :mod:`retopo_agent.geometry.decimate`;
- a Blender adapter that prefers QuadriFlow Remesh and projects the result back
  onto the high-poly with Shrinkwrap -- see :mod:`retopo_agent.blender.retopo`;
- synthetic high-poly fixtures -- see :mod:`retopo_agent.io.fixtures`.

The mesh representation is shared with the UV agent so both engines speak the
same graph (:class:`uv_agent.geometry.mesh_graph.MeshGraph`).
"""
