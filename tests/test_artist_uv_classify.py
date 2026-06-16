"""A2 descriptors + A3 classification — Blender-free unit tests (AUTO_ARTIST_UV_PLAN
§5.A2/§5.A3 / §AR3)."""

import math

import numpy as np

from artist_uv_agent.classification import PART_TYPES, classify_part, classify_parts
from artist_uv_agent.descriptors import (
    PartDescriptor, _boundary_loop_count, _pca, describe_parts, detect_symmetry,
)
from artist_uv_agent.segmentation import segment_parts
from chart_uv_agent.fixtures import build_humanoid_blob
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_grid_plane


# -- fixtures ---------------------------------------------------------------

def _cylinder(seg=20, h=12, rad=1.0, height=6.0):
    verts = []
    for k in range(h + 1):
        z = k / h * height
        for s in range(seg):
            a = 2 * math.pi * s / seg
            verts.append((rad * math.cos(a), rad * math.sin(a), z))
    faces = []
    for k in range(h):
        for s in range(seg):
            s2 = (s + 1) % seg
            faces.append((k * seg + s, k * seg + s2, (k + 1) * seg + s2, (k + 1) * seg + s))
    return MeshGraph.from_faces("cyl", verts, faces)


def _long_strip(nx=24, ny=3, w=8.0, h=1.0):
    verts = [(i / nx * w, j / ny * h, 0.0) for j in range(ny + 1) for i in range(nx + 1)]
    faces = [(j * (nx + 1) + i, j * (nx + 1) + i + 1,
              (j + 1) * (nx + 1) + i + 1, (j + 1) * (nx + 1) + i)
             for j in range(ny) for i in range(nx)]
    return MeshGraph.from_faces("strip", verts, faces)


def _classify(mesh):
    seg = segment_parts(mesh)
    descs = describe_parts(mesh, seg)
    cls = classify_parts(descs, {p.part_id: p.neighbors for p in seg.parts})
    return descs, cls


# -- descriptors ------------------------------------------------------------

def test_pca_extents_descending_and_oriented():
    # A box 4×2×1 → extents sorted [4,2,1] along the principal axes.
    pts = np.array([[x, y, z] for x in (-2, 2) for y in (-1, 1) for z in (-0.5, 0.5)], float)
    axes, ext, c = _pca(pts)
    assert ext[0] > ext[1] > ext[2]
    assert abs(ext[0] - 4) < 1e-6 and abs(ext[2] - 1) < 1e-6


def test_boundary_loops_disk_vs_tube():
    plane = build_grid_plane(nx=4, ny=4)               # a disk → 1 loop
    assert _boundary_loop_count(plane, [f.id for f in plane.faces]) == 1
    tube = _cylinder()                                  # open tube → 2 loops
    assert _boundary_loop_count(tube, [f.id for f in tube.faces]) == 2


def test_panel_descriptor_is_flat():
    plane = build_grid_plane(nx=8, ny=8)
    descs, _ = _classify(plane)
    d = descs[0]
    assert d.flatness < 0.05 and d.normal_cone_deg < 5.0 and d.is_disk


def test_symmetry_pairs_two_mirror_parts():
    # Two identical square panels at x=±3 → mirror mates across the x-plane.
    d0 = PartDescriptor(0, 1.0, 4, (-3, 0, 0), [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                        (1, 1, 0.01), 1, 0.01, 0, 1, 0, 1, True, 0.5, 0.5)
    d1 = PartDescriptor(1, 1.0, 4, (3, 0, 0), [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                        (1, 1, 0.01), 1, 0.01, 0, 1, 0, 1, True, 0.5, 0.5)
    detect_symmetry([d0, d1], mesh_centroid=np.zeros(3))
    assert d0.symmetry_mate == 1 and d1.symmetry_mate == 0


# -- classification ---------------------------------------------------------

def test_classify_panel():
    _, cls = _classify(build_grid_plane(nx=8, ny=8))
    assert cls[0].type == "panel"


def test_classify_cylinder():
    descs, cls = _classify(_cylinder())
    assert any(c.type == "cylinder" for c in cls)


def test_classify_strip():
    _, cls = _classify(_long_strip())
    assert any(c.type == "strip" for c in cls)


def test_classify_blob_body():
    descs, cls = _classify(build_humanoid_blob())
    big = max(zip(descs, cls), key=lambda x: x[0].area)[1]
    assert big.type == "blob"


def test_all_types_known_and_confidence_unit():
    for mesh in (build_grid_plane(nx=6, ny=6), _cylinder(), build_humanoid_blob()):
        _, cls = _classify(mesh)
        for c in cls:
            assert c.type in PART_TYPES
            assert 0.0 <= c.confidence <= 1.0


def test_unknown_is_explicit_for_ambiguous_part():
    """A part matching no rule falls to an EXPLICIT 'unknown' (→ chart fallback), never a
    silent mislabel."""
    d = PartDescriptor(0, 1.0, 50, (0, 0, 0), [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                       (2.0, 1.6, 0.9), elongation=1.25, flatness=0.56, stripness=0.0,
                       cylindricalness=0.4, normal_cone_deg=60.0, boundary_loops=3,
                       is_disk=False, extremity=0.3, area_frac=0.3, confidence=0.4)
    c = classify_part(d, seg_neighbors={1, 2})
    assert c.type == "unknown"
