"""Artist-style UV engine (AUTO_ARTIST_UV_PLAN).

A no-reference UV engine whose goal is *artist-style* UVs — readable semantic parts,
purposeful seams, consistent orientation, a layout grammar, and coherent texel density —
rather than merely *valid generic* UVs (the job of :mod:`chart_uv_agent`).

Pipeline (plan §5):

    low-poly mesh
      → A1 semantic part segmentation      (segmentation.segment_parts)
      → A2 per-part descriptors            (descriptors.describe_parts)
      → A3 part classification             (classification.classify_parts)
      → A4 seam templates per part type    (seams.part_seams)
      → A5 unwrap with SLIM                 (pipeline.run_artist_uv, Blender)
      → A6 layout grammar / orientation     (layout.plan_layout / band_shelf_pack)
      → A7 importance-based texel density   (density.density_weights)
      → gate + artist-style report          (gate.evaluate_artist_gate / artist_report)

Every module except :mod:`artist_uv_agent.pipeline`'s Blender entry point is pure
Python on a :class:`~uv_agent.geometry.mesh_graph.MeshGraph` / ``UVMap`` so it is unit
testable without ``bpy`` (the Blender SLIM unwrap / render live in the pipeline only).

``artist`` is NOT the default engine yet (plan §8): ``--uv-engine chart`` remains the
stable generic fallback and ``--uv-engine transfer`` the explicit reference-assisted
mode. The artist engine targets organic / statue-like assets first; hard-surface
support is experimental (plan §10).
"""

from __future__ import annotations

__all__ = ["ENGINE_NAME"]

ENGINE_NAME = "artist"
