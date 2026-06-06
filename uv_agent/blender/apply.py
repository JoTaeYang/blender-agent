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
