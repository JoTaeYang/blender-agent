"""Tests for split_smoothing_by_uv_islands (apply_smoothing_split_by_edges).

The function is intentionally bpy-free (it only touches ``obj.data``), so we can
exercise its full logic with a fake Blender mesh -- no Blender required.
"""

from uv_agent.blender.apply import apply_smoothing_split_by_edges


class _FakeEdge:
    def __init__(self, index, sharp=False):
        self.index = index
        self.use_edge_sharp = sharp


class _FakePoly:
    def __init__(self, smooth=False):
        self.use_smooth = smooth


class _FakeMesh:
    def __init__(self, n_edges, n_polys, presharp=()):
        self.edges = [_FakeEdge(i, i in set(presharp)) for i in range(n_edges)]
        self.polygons = [_FakePoly() for _ in range(n_polys)]
        self.updated = False

    def update(self):
        self.updated = True


class _FakeObj:
    def __init__(self, mesh):
        self.data = mesh


def test_function_is_importable_without_bpy():
    # Importing the module + symbol must not require bpy.
    assert callable(apply_smoothing_split_by_edges)


def test_empty_edge_ids_is_safe():
    mesh = _FakeMesh(n_edges=12, n_polys=6)
    obj = _FakeObj(mesh)
    count = apply_smoothing_split_by_edges(obj, [])
    assert count == 0
    assert all(not e.use_edge_sharp for e in mesh.edges)
    # smooth_faces default True -> faces become smooth
    assert all(p.use_smooth for p in mesh.polygons)
    assert mesh.updated is True


def test_none_edge_ids_is_safe():
    mesh = _FakeMesh(n_edges=12, n_polys=6)
    assert apply_smoothing_split_by_edges(_FakeObj(mesh), None) == 0


def test_marks_only_given_edges_sharp():
    mesh = _FakeMesh(n_edges=12, n_polys=6)
    count = apply_smoothing_split_by_edges(_FakeObj(mesh), [1, 3, 7])
    assert count == 3
    sharp = {e.index for e in mesh.edges if e.use_edge_sharp}
    assert sharp == {1, 3, 7}


def test_preserves_existing_sharp_edges():
    # Edge 0 was already sharp (user authored); we must not clear it.
    mesh = _FakeMesh(n_edges=12, n_polys=6, presharp=[0])
    count = apply_smoothing_split_by_edges(_FakeObj(mesh), [5])
    assert count == 1  # only the edge we marked is counted
    sharp = {e.index for e in mesh.edges if e.use_edge_sharp}
    assert sharp == {0, 5}  # existing sharp edge 0 preserved


def test_smooth_faces_false_leaves_faces_untouched():
    mesh = _FakeMesh(n_edges=12, n_polys=6)
    apply_smoothing_split_by_edges(_FakeObj(mesh), [2], smooth_faces=False)
    assert all(not p.use_smooth for p in mesh.polygons)
    assert {e.index for e in mesh.edges if e.use_edge_sharp} == {2}


def test_out_of_range_edge_ids_ignored():
    mesh = _FakeMesh(n_edges=6, n_polys=2)
    count = apply_smoothing_split_by_edges(_FakeObj(mesh), [99, 100])
    assert count == 0
    assert all(not e.use_edge_sharp for e in mesh.edges)
