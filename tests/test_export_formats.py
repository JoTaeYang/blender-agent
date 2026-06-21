"""Pure-helper tests for the Blender export module (MVP 5 plan §5, Session B).

``uv_agent/blender/export.py`` imports ``bpy`` lazily, so its axis-mapping and
dispatch helpers import + test without Blender. The actual FBX/OBJ/GLB export is
exercised by the Blender-gated e2e smoke (``tests/e2e/test_mvp5_export.py``).
"""

from uv_agent.blender import export


def test_obj_axis_enum_maps_fbx_tokens():
    assert export.obj_axis_enum("-Z") == "NEGATIVE_Z"
    assert export.obj_axis_enum("Y") == "Y"
    assert export.obj_axis_enum("-X") == "NEGATIVE_X"
    assert export.obj_axis_enum("z") == "Z"  # case-insensitive
    assert export.obj_axis_enum("-y") == "NEGATIVE_Y"


def test_obj_axis_enum_unknown_falls_back_to_default():
    assert export.obj_axis_enum(None, "Y") == "Y"
    assert export.obj_axis_enum("bogus", "-Z") == "NEGATIVE_Z"
    assert export.obj_axis_enum("", "X") == "X"


def test_export_dispatch_covers_all_formats():
    assert set(export.EXPORT_DISPATCH.keys()) == {"fbx", "obj", "glb", "gltf"}


def test_default_axes_match_contract():
    assert export.DEFAULT_AXIS_FORWARD == "-Z"
    assert export.DEFAULT_AXIS_UP == "Y"
