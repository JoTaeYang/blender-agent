"""Blender adapter for the DM3 component budget planner (decimation plan §6).

Extracts a :class:`~uv_agent.geometry.mesh_graph.MeshGraph` from a Blender object
and runs the pure-geometry :func:`retopo_agent.geometry.component_budget.plan_component_budget`
on it, so the budget logic is shared with the offline unit tests.

Like the DM2 diagnosis, this targets the *decimated result* (small enough for a
Python graph), where the detached-component structure that blocks a lower target
is visible. ``max_graph_faces`` guards against an accidental call on an over-large
mesh. Only runs inside Blender (graph extraction touches ``bpy`` data lazily).
"""

from __future__ import annotations


def plan_component_budget_blender(
    obj,
    target_face_count: int,
    *,
    policy: str = "preserve_all",
    allow_removal: bool = False,
    max_graph_faces: int = 2_000_000,
    **kwargs,
):
    """Plan the per-component face budget for ``obj`` (plan §6).

    Returns a :class:`~retopo_agent.geometry.component_budget.ComponentBudgetReport`,
    or ``None`` if the mesh exceeds ``max_graph_faces``. ``kwargs`` forward to
    ``plan_component_budget`` (``min_shell_faces``, ``importance_weights``, ...).
    """
    from retopo_agent.geometry.component_budget import plan_component_budget
    from uv_agent.blender.extract import extract_mesh_graph

    if len(obj.data.polygons) > max_graph_faces:
        return None
    mesh = extract_mesh_graph(obj)
    return plan_component_budget(
        mesh, target_face_count, policy=policy, allow_removal=allow_removal, **kwargs
    )
