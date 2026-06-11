"""Direct UV write-back into Blender (plan §6, §7.4 / Phase 1).

This is the project's defining capability: the agent writes UV coordinates
straight into a mesh's UV layer instead of relying solely on Blender's unwrap
operator.

    uv_layer = mesh.uv_layers["AI_UV"].data
    uv_layer[loop_index].uv = (u, v)
"""

from __future__ import annotations

from uv_agent.geometry.solution import UVSolution

AI_UV_LAYER = "AI_UV"


def apply_uv_coordinates(obj, solution: UVSolution, *, layer_name: str = AI_UV_LAYER, seam_edge_ids=None):
    """Write a :class:`UVSolution` into the object's UV layer (created if needed).

    Returns the number of loops written. Only runs inside Blender.
    """
    mesh = obj.data

    uv_layers = mesh.uv_layers
    if layer_name in uv_layers:
        uv_layer = uv_layers[layer_name]
    else:
        uv_layer = uv_layers.new(name=layer_name)
    uv_layers.active = uv_layer

    data = uv_layer.data
    n_loops = len(data)
    written = 0
    for entry in solution.uv_coordinates:
        li = entry["loop_index"]
        if 0 <= li < n_loops:  # validate loop index (plan §7.4)
            u, v = entry["uv"]
            data[li].uv = (float(u), float(v))
            written += 1

    if seam_edge_ids:
        seam = set(seam_edge_ids)
        for e in mesh.edges:
            e.use_seam = e.index in seam

    mesh.update()
    return written


def apply_smoothing_split_by_edges(obj, edge_ids, *, smooth_faces: bool = True) -> int:
    """Split normal smoothing at the given edges (the UV island boundaries).

    Implements the ``split_smoothing_by_uv_islands`` feature. Blender does not
    manage 3ds-Max-style smoothing group ids; a normal split is expressed by a
    *sharp edge* on top of *smooth faces*. So this:

    - (optionally) sets every face to smooth shading, so island interiors stay
      smooth and shading actually splits at the marked edges;
    - marks each edge in ``edge_ids`` as sharp (``use_edge_sharp = True``),
      which is where the UV islands meet, so normals break there.

    Existing sharp edges are preserved (we only add, never clear) -- clearing is
    left to a future ``clear_previous_ai_sharp_edges`` option. Passing an empty
    / ``None`` ``edge_ids`` is safe and only (optionally) re-smooths faces.

    Pure ``obj.data`` access (no ``bpy`` import) so it is unit-testable with a
    fake mesh; it only does something meaningful inside Blender.

    Returns the number of edges marked sharp.
    """
    mesh = obj.data
    edge_set = set(edge_ids or [])

    if smooth_faces:
        for poly in mesh.polygons:
            poly.use_smooth = True

    sharp_count = 0
    for e in mesh.edges:
        if e.index in edge_set:
            e.use_edge_sharp = True
            sharp_count += 1

    mesh.update()
    return sharp_count


def apply_checker_material(obj, *, name: str = "AI_UV_Checker"):
    """Attach a checker-texture material so the UV layout is visible in renders
    (plan §7.4 "checker material 적용"). Only runs inside Blender."""
    import bpy

    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name=name)
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        bsdf = nt.nodes.new("ShaderNodeBsdfDiffuse")
        checker = nt.nodes.new("ShaderNodeTexChecker")
        checker.inputs["Scale"].default_value = 16.0
        nt.links.new(checker.outputs["Color"], bsdf.inputs["Color"])
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return mat
