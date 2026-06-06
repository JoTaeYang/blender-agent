"""Preview rendering + that the Blender adapter imports without bpy installed."""

from uv_agent.geometry.packing import pack_islands
from uv_agent.geometry.preview import uv_layout_svg
from uv_agent.geometry.projection import project_island
from uv_agent.geometry.solution import UVMap
from uv_agent.io import fixtures
from uv_agent.planner.island_planner import plan_islands


def test_svg_preview_is_wellformed():
    m = fixtures.build_cube()
    plan = plan_islands(m)
    uvm = UVMap.for_mesh(m)
    for isl in plan.islands:
        project_island(m, isl.face_ids, uvm, isl.projection)
    pack_islands(m, plan, uvm)
    svg = uv_layout_svg(m, plan, uvm, title="cube")
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    # One polygon per face.
    assert svg.count("<polygon") == m.face_count


def test_blender_adapter_imports_without_bpy():
    # The adapter must be importable outside Blender (lazy bpy import).
    import uv_agent.blender.apply as apply_mod
    import uv_agent.blender.extract as extract_mod

    assert hasattr(extract_mod, "extract_mesh_graph")
    assert hasattr(apply_mod, "apply_uv_coordinates")
    assert apply_mod.AI_UV_LAYER == "AI_UV"
