"""Tests for UV review image artifacts (plan §7, Session C).

The pure-Python SVG layout fallback and module import-safety are tested
everywhere; the Blender render smoke (``uv.export_layout`` + checker render) is
gated behind a Blender install so ``pytest`` stays green without one.
"""

import importlib
import os
import xml.etree.ElementTree as ET

import pytest

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.geometry.uv_review import rasterize_uv_layout, uv_layout_svg, write_uv_layout_png


def _two_island_mesh():
    mesh = MeshGraph.from_faces(
        "two",
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
         (5, 0, 0), (6, 0, 0), (6, 1, 0), (5, 1, 0)],
        [[0, 1, 2, 3], [4, 5, 6, 7]],
    )
    uvm = UVMap.for_mesh(mesh)
    for li, uv in zip(mesh.faces[0].loop_indices, [(0, 0), (0.4, 0), (0.4, 0.4), (0, 0.4)]):
        uvm.set(li, *uv)
    for li, uv in zip(mesh.faces[1].loop_indices, [(0.6, 0.6), (1, 0.6), (1, 1), (0.6, 1)]):
        uvm.set(li, *uv)
    return mesh, uvm


def test_uv_layout_svg_is_wellformed():
    mesh, uvm = _two_island_mesh()
    svg = uv_layout_svg(mesh, uvm, title="pot · UVChannel_1")
    root = ET.fromstring(svg)  # raises on malformed XML
    assert root.tag.endswith("svg")
    polys = [e for e in root.iter() if e.tag.endswith("polygon")]
    assert len(polys) == 2  # one per face
    assert "pot" in svg


def test_uv_layout_svg_clamps_out_of_bounds():
    mesh, uvm = _two_island_mesh()
    # Push a corner far out of the tile; the SVG must still be valid (clamped).
    uvm.set(mesh.faces[0].loop_indices[0], 9.0, -9.0)
    svg = uv_layout_svg(mesh, uvm)
    ET.fromstring(svg)


def test_uv_layout_png_is_valid_headless(tmp_path):
    # The layout PNG must be produced without Blender/GPU (plan §7, §13).
    mesh, uvm = _two_island_mesh()
    canvas = rasterize_uv_layout(mesh, uvm, size=128)
    assert canvas.shape == (128, 128, 4)
    assert canvas.dtype.name == "uint8"
    path = str(tmp_path / "uv_layout.png")
    write_uv_layout_png(mesh, uvm, path, size=128)
    assert os.path.getsize(path) > 0
    with open(path, "rb") as fh:
        assert fh.read(8) == b"\x89PNG\r\n\x1a\n"  # PNG magic


def test_review_render_imports_without_blender():
    # review_render only imports bpy lazily inside functions.
    mod = importlib.import_module("uv_agent.blender.review_render")
    assert hasattr(mod, "render_checker_views")
    assert hasattr(mod, "apply_checker_material")


def test_worker_module_imports_without_blender():
    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    path = os.path.join(root, "worker", "review_existing_uv.py")
    spec = importlib.util.spec_from_file_location("review_existing_uv", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # must not import bpy at module load
    assert hasattr(mod, "main")


# --- Blender-gated render smoke -------------------------------------------
_COMMON_BLENDER = [
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "/usr/bin/blender",
    "/usr/local/bin/blender",
]


def _has_blender() -> bool:
    import shutil

    if os.environ.get("BLENDER") and os.path.exists(os.environ["BLENDER"]):
        return True
    if shutil.which("blender"):
        return True
    return any(os.path.exists(p) for p in _COMMON_BLENDER)


@pytest.mark.skipif(not _has_blender(), reason="Blender not installed")
def test_render_smoke_via_blender(tmp_path):
    """End-to-end headless render smoke: layout PNG + checker for a unit quad.

    Runs a tiny ``blender --background`` script so the real read-UV -> rasterize
    layout -> EEVEE checker render path is exercised exactly as the worker runs it
    (plan Session C acceptance). The layout is the headless rasterizer, not the
    GPU-only ``uv.export_layout`` operator.
    """
    import json
    import shutil
    import subprocess

    blender = os.environ.get("BLENDER") or shutil.which("blender") or next(
        p for p in _COMMON_BLENDER if os.path.exists(p))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = str(tmp_path)
    script = os.path.join(out_dir, "smoke.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(
            "import sys, os, json, bpy\n"
            f"sys.path.insert(0, {root!r})\n"
            "from uv_agent.blender.uv_extract import extract_mesh_graph_with_uv\n"
            "from uv_agent.blender.review_render import render_checker_views\n"
            "from uv_agent.geometry.uv_review import write_uv_layout_png\n"
            "bpy.ops.wm.read_homefile(use_empty=True)\n"
            "bpy.ops.mesh.primitive_plane_add()\n"
            "obj = bpy.context.active_object\n"
            "bpy.ops.object.mode_set(mode='EDIT'); bpy.ops.uv.smart_project(); bpy.ops.object.mode_set(mode='OBJECT')\n"
            "mesh, uvmap, layer = extract_mesh_graph_with_uv(obj)\n"
            f"png = write_uv_layout_png(mesh, uvmap, {os.path.join(out_dir, 'uv_layout.png')!r}, size=256)\n"
            f"chk = render_checker_views(obj, {out_dir!r}, size=128)\n"
            f"open({os.path.join(out_dir, 'result.json')!r}, 'w').write(json.dumps({{'png': png, 'layer': layer, 'chk': chk}}))\n"
        )
    proc = subprocess.run(
        [blender, "--background", "--python", script],
        capture_output=True, text=True, timeout=300)
    result_path = os.path.join(out_dir, "result.json")
    assert os.path.exists(result_path), f"no result\nstdout={proc.stdout}\nstderr={proc.stderr}"
    result = json.loads(open(result_path, encoding="utf-8").read())
    assert result["png"] and os.path.getsize(result["png"]) > 0
    assert result["layer"]  # smart_project created a UV layer that read back
    assert "front" in result["chk"] and "side" in result["chk"]
