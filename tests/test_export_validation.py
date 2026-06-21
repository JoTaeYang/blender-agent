"""Pure-helper tests for export validation (MVP 5 plan §7, Session C).

``uv_agent/blender/export_validation.py`` imports ``bpy`` lazily, so its warning
policy helpers import + test without Blender. The actual re-open validation is
exercised by the Blender-gated e2e smoke (``tests/e2e/test_mvp5_export.py``).
"""

from uv_agent.blender import export_validation as ev


# --- UV layer naming tolerance (plan §7) -----------------------------------
def test_uv_layer_warning_when_expected_name_absent():
    w = ev.uv_layer_warnings(["UVMap"], "AI_UV", fmt="obj")
    assert len(w) == 1 and "AI_UV" in w[0]


def test_uv_layer_no_warning_when_present_or_unspecified():
    assert ev.uv_layer_warnings(["AI_UV"], "AI_UV", fmt="fbx") == []
    assert ev.uv_layer_warnings(["UVMap"], None, fmt="obj") == []
    assert ev.uv_layer_warnings([], "AI_UV", fmt="glb") == []  # no UV -> caller hard-fails, not here


# --- face/vertex drift tolerance (plan §7) ---------------------------------
def test_face_drift_warns_only_when_not_triangulated():
    # big face delta, not triangulated -> warn
    w = ev.count_warnings(fmt="glb", faces=20000, vertices=6562,
                          source_faces=12152, source_vertices=6562, triangulated=False)
    assert any("face count" in m for m in w)
    # same delta, triangulated -> no face warning (expected change)
    w2 = ev.count_warnings(fmt="glb", faces=24304, vertices=6562,
                           source_faces=12152, source_vertices=6562, triangulated=True)
    assert not any("face count" in m for m in w2)


def test_vertex_drift_only_warns_when_large():
    # small vertex split (format round-trip) -> no warning
    assert ev.count_warnings(fmt="fbx", faces=12152, vertices=6800,
                             source_faces=12152, source_vertices=6562, triangulated=False) == []
    # huge vertex blow-up -> warn
    w = ev.count_warnings(fmt="fbx", faces=12152, vertices=20000,
                          source_faces=12152, source_vertices=6562, triangulated=False)
    assert any("vertex count" in m for m in w)


def test_count_warnings_tolerates_missing_source():
    assert ev.count_warnings(fmt="obj", faces=10, vertices=10,
                             source_faces=None, source_vertices=None, triangulated=False) == []


# --- normals tolerance (plan §7) -------------------------------------------
def test_normals_warning_only_when_requested_and_absent():
    assert ev.normals_warning(False, True, fmt="obj") != []
    assert ev.normals_warning(True, True, fmt="obj") == []
    assert ev.normals_warning(False, False, fmt="obj") == []
