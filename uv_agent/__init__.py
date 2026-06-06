"""AI Direct UV Layout Agent.

A deterministic UV geometry engine plus an LLM agent layer for Blender.

Architecture (per the project plan):

    LLM    = intent understanding + strategy + repair planning
    Solver = coordinate computation + constraint optimization + validation
    Blender = mesh source + UV write + preview render

The :mod:`uv_agent.geometry`, :mod:`uv_agent.planner`, :mod:`uv_agent.agent`
packages are pure Python (numpy only) and run without Blender, so the whole
plan -> generate -> pack -> evaluate -> repair loop is unit-testable.

The :mod:`uv_agent.blender` package is a thin adapter that lazily imports
``bpy``/``bmesh`` and only works inside Blender.
"""

from __future__ import annotations

__version__ = "0.1.0"

from uv_agent.geometry.mesh_graph import Edge, Face, Loop, MeshGraph, Vertex
from uv_agent.geometry.solution import UVMap, UVSolution

__all__ = [
    "__version__",
    "MeshGraph",
    "Vertex",
    "Edge",
    "Face",
    "Loop",
    "UVMap",
    "UVSolution",
]
