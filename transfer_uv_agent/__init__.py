"""Reference-Guided UV Transfer engine (UV_TRANSFER_PLAN).

A third P5 UV engine (``--uv-engine transfer``) that transfers a UV'd reference
asset's chart LAYOUT onto the adaptive low-poly mesh, instead of generating charts
geometrically (the chart engine) — so the result is *semantically* like the artist
design (head/arms/torso/cloth in matching slots), which the geometric engine cannot
reproduce by construction.

Pipeline (plan §3): T1 extract reference charts + BVH → T2 project chart ids onto the
adaptive mesh (normal-compat + distance guards + speckle cleanup) → T3 seams + SLIM
unwrap → T4 reference-guided placement (density-matched scale, IoU rotation, slot
translation) → T5 hard gates (raster/flip/bounds/no-fallback) + correspondence report.

The pure cores (``reference``, ``projection``, ``placement``, ``gate``) are Blender-free
and unit-tested; ``pipeline`` is the Blender orchestration (``bpy``/BVHTree, lazy).
"""
