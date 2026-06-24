"""UV boundary -> seam extraction tests (Electron MVP 2 plan §6.4, Session B).

Blender-free: builds a tiny two-quad :class:`MeshGraph` plus a hand-authored
:class:`UVMap` so the UV discontinuity is known exactly, and asserts
:func:`uv_agent.geometry.uv_boundary.extract_uv_boundary_seams` flags the shared
edge as a seam only when the UV is actually cut there.
"""

from artist_uv_agent.user_seams import UserSeamSpec
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.geometry.uv_boundary import extract_uv_boundary_seams

# Two quads sharing edge (1, 2):
#   A = [0,1,2,3]   B = [1,4,5,2]
_VERTS = [
    (0.0, 0.0, 0.0),  # 0
    (1.0, 0.0, 0.0),  # 1
    (1.0, 1.0, 0.0),  # 2
    (0.0, 1.0, 0.0),  # 3
    (2.0, 0.0, 0.0),  # 4
    (2.0, 1.0, 0.0),  # 5
]
_FACES = [[0, 1, 2, 3], [1, 4, 5, 2]]


def _two_quads() -> MeshGraph:
    return MeshGraph.from_faces("two_quads", _VERTS, _FACES)


def _set_loop_uv(mesh: MeshGraph, uvmap: UVMap, face_id: int, vertex_id: int, u: float, v: float):
    face = mesh.faces[face_id]
    for li in face.loop_indices:
        if mesh.loops[li].vertex_id == vertex_id:
            uvmap.set(li, u, v)
            return
    raise AssertionError(f"vertex {vertex_id} not a corner of face {face_id}")


def _shared_edge_id(mesh: MeshGraph) -> int:
    return mesh.edge_key(1, 2)


def test_continuous_uv_has_no_boundary_seam():
    """Both quads packed contiguously: the shared edge is NOT a seam."""
    mesh = _two_quads()
    uvmap = UVMap.for_mesh(mesh)
    # Face A in [0,1]^2; Face B continues to the right sharing v1/v2 UVs exactly.
    _set_loop_uv(mesh, uvmap, 0, 0, 0.0, 0.0)
    _set_loop_uv(mesh, uvmap, 0, 1, 1.0, 0.0)
    _set_loop_uv(mesh, uvmap, 0, 2, 1.0, 1.0)
    _set_loop_uv(mesh, uvmap, 0, 3, 0.0, 1.0)
    _set_loop_uv(mesh, uvmap, 1, 1, 1.0, 0.0)  # same as face A's v1
    _set_loop_uv(mesh, uvmap, 1, 4, 2.0, 0.0)
    _set_loop_uv(mesh, uvmap, 1, 5, 2.0, 1.0)
    _set_loop_uv(mesh, uvmap, 1, 2, 1.0, 1.0)  # same as face A's v2

    res = extract_uv_boundary_seams(mesh, uvmap)
    assert _shared_edge_id(mesh) not in res.seam_edges
    assert res.seam_edges == []  # the only interior edge is continuous


def test_discontinuous_uv_is_a_boundary_seam():
    """Face B's island is placed elsewhere: the shared edge IS a seam."""
    mesh = _two_quads()
    uvmap = UVMap.for_mesh(mesh)
    _set_loop_uv(mesh, uvmap, 0, 0, 0.0, 0.0)
    _set_loop_uv(mesh, uvmap, 0, 1, 1.0, 0.0)
    _set_loop_uv(mesh, uvmap, 0, 2, 1.0, 1.0)
    _set_loop_uv(mesh, uvmap, 0, 3, 0.0, 1.0)
    # Face B placed in a separate UV island -> v1/v2 disagree across the edge.
    _set_loop_uv(mesh, uvmap, 1, 1, 5.0, 0.0)
    _set_loop_uv(mesh, uvmap, 1, 4, 6.0, 0.0)
    _set_loop_uv(mesh, uvmap, 1, 5, 6.0, 1.0)
    _set_loop_uv(mesh, uvmap, 1, 2, 5.0, 1.0)

    res = extract_uv_boundary_seams(mesh, uvmap)
    assert _shared_edge_id(mesh) in res.seam_edges
    assert res.boundary_edge_count == 1


def test_one_endpoint_discontinuity_is_enough():
    """If EITHER endpoint's UV is cut, the edge is a seam (plan §6.4)."""
    mesh = _two_quads()
    uvmap = UVMap.for_mesh(mesh)
    _set_loop_uv(mesh, uvmap, 0, 0, 0.0, 0.0)
    _set_loop_uv(mesh, uvmap, 0, 1, 1.0, 0.0)
    _set_loop_uv(mesh, uvmap, 0, 2, 1.0, 1.0)
    _set_loop_uv(mesh, uvmap, 0, 3, 0.0, 1.0)
    _set_loop_uv(mesh, uvmap, 1, 1, 1.0, 0.0)   # v1 continuous
    _set_loop_uv(mesh, uvmap, 1, 4, 2.0, 0.0)
    _set_loop_uv(mesh, uvmap, 1, 5, 2.0, 1.0)
    _set_loop_uv(mesh, uvmap, 1, 2, 9.0, 9.0)   # v2 cut

    res = extract_uv_boundary_seams(mesh, uvmap)
    assert _shared_edge_id(mesh) in res.seam_edges


def test_mesh_boundary_edges_reported_not_seamed():
    mesh = _two_quads()
    uvmap = UVMap.for_mesh(mesh)  # all-zero UVs -> shared edge continuous
    res = extract_uv_boundary_seams(mesh, uvmap)
    # The 6 outer edges each touch one face -> reported, never auto-seamed.
    assert len(res.mesh_boundary_edges) == 6
    assert _shared_edge_id(mesh) not in res.mesh_boundary_edges
    report = res.report()
    assert report["boundary_edge_count"] == 0
    assert report["uv_layer_missing"] is False
    # MVP3 §2 Goal A: the report explains a low boundary count via island_count + method.
    assert report["island_count"] == 1            # continuous UVs -> one welded island
    assert report["mesh_boundary_edge_count"] == 6
    assert report["method"] == "uv_loop_discontinuity"


def test_report_island_count_grows_when_uv_is_cut():
    """A cut UV yields two islands; the report's island_count reflects it (Goal A)."""
    mesh = _two_quads()
    uvmap = UVMap.for_mesh(mesh)
    _set_loop_uv(mesh, uvmap, 0, 0, 0.0, 0.0)
    _set_loop_uv(mesh, uvmap, 0, 1, 1.0, 0.0)
    _set_loop_uv(mesh, uvmap, 0, 2, 1.0, 1.0)
    _set_loop_uv(mesh, uvmap, 0, 3, 0.0, 1.0)
    _set_loop_uv(mesh, uvmap, 1, 1, 5.0, 0.0)
    _set_loop_uv(mesh, uvmap, 1, 4, 6.0, 0.0)
    _set_loop_uv(mesh, uvmap, 1, 5, 6.0, 1.0)
    _set_loop_uv(mesh, uvmap, 1, 2, 5.0, 1.0)
    res = extract_uv_boundary_seams(mesh, uvmap)
    assert res.island_count == 2
    assert res.report()["island_count"] == 2


def test_extracted_spec_loads_as_user_seam_spec():
    """A spec built from the extracted boundary loads via UserSeamSpec (acceptance B)."""
    mesh = _two_quads()
    uvmap = UVMap.for_mesh(mesh)
    _set_loop_uv(mesh, uvmap, 0, 1, 1.0, 0.0)
    _set_loop_uv(mesh, uvmap, 0, 2, 1.0, 1.0)
    _set_loop_uv(mesh, uvmap, 1, 1, 5.0, 0.0)
    _set_loop_uv(mesh, uvmap, 1, 2, 5.0, 1.0)
    res = extract_uv_boundary_seams(mesh, uvmap)

    spec_dict = {
        "version": 1, "object": "two_quads", "mode": "user_seams",
        "mandatory_fold_angle": 90.0,
        "user_seam_edges": res.seam_edges, "user_protected_edges": [],
        "chapters": [], "notes": "Extracted from UV island boundaries",
    }
    spec = UserSeamSpec.from_dict(spec_dict)
    assert spec.user_seam_edges == set(res.seam_edges)
    assert spec.mode == "user_seams"


def _load_generate_contract():
    """Load the MVP 3 generate contract stand-alone (no Blender, no chart engine)."""
    import importlib.util
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "worker", "app_uv_generate_contract.py")
    spec = importlib.util.spec_from_file_location("app_uv_generate_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_uv_boundary_makes_a_derived_seam_spec_with_no_protected_edges():
    """Boundary edges -> ``make_derived_seam_spec`` -> ``UserSeamSpec`` round-trip.

    The UV-boundary fallback (revision plan §4.3, §6.1) turns the extracted island
    boundary directly into a canonical derived spec with an empty protected set —
    the same path the Generate worker takes when no MVP 2 spec exists.
    """
    contract = _load_generate_contract()
    mesh = _two_quads()
    uvmap = UVMap.for_mesh(mesh)
    _set_loop_uv(mesh, uvmap, 0, 1, 1.0, 0.0)
    _set_loop_uv(mesh, uvmap, 0, 2, 1.0, 1.0)
    _set_loop_uv(mesh, uvmap, 1, 1, 5.0, 0.0)
    _set_loop_uv(mesh, uvmap, 1, 2, 5.0, 1.0)
    res = extract_uv_boundary_seams(mesh, uvmap)
    assert res.seam_edges  # the shared edge is cut -> a boundary seam

    derived = contract.make_derived_seam_spec(
        object_name="two_quads", user_seam_edges=res.seam_edges, uv_layer="UVChannel_1")
    spec = UserSeamSpec.from_dict(derived)
    assert spec.user_seam_edges == set(res.seam_edges)
    assert spec.user_protected_edges == set()  # revision plan §6.1
    assert "UVChannel_1" in derived["notes"]
