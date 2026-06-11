"""Phase A2 — adaptive decimation on the proxy (Adaptive Low-Poly plan §5).

These exercise the Blender-free decision logic of the adaptive-mode generator: the
tri/quad/n-gon split that enforces the **0-n-gon (hard)** invariant and the natural
tri/quad mix, and the "keep the shrinkwrap snap only if it improved shape" rule.
The ``bpy``-touching entry (:func:`adaptive_decimate_proxy`) is covered by the
headless ``@pytest.mark.blender`` integration test; here we test the pure core, the
same offline pattern used for the QuadriFlow / Collapse searches.
"""

from retopo_agent.blender.adaptive_decimate import (
    AdaptiveAttempt,
    cleanup_asserts,
    face_type_breakdown,
    shrinkwrap_improves,
)


def test_face_type_breakdown_counts_tris_quads_ngons():
    bd = face_type_breakdown([3, 3, 4, 4, 4, 5, 6])
    assert bd["tris"] == 2
    assert bd["quads"] == 3
    assert bd["ngons"] == 2
    assert bd["faces"] == 7
    assert bd["quad_ratio"] == round(3 / 7, 4)


def test_face_type_breakdown_reference_style_mix_has_zero_ngons():
    # The ground-truth humanstatue_low is 5,799 tris + 51 quads, 0 n-gons (plan §1).
    bd = face_type_breakdown([3] * 5799 + [4] * 51)
    assert bd["faces"] == 5850
    assert bd["tris"] == 5799
    assert bd["quads"] == 51
    assert bd["ngons"] == 0  # adaptive-mode hard invariant


def test_face_type_breakdown_empty_mesh():
    bd = face_type_breakdown([])
    assert bd == {"faces": 0, "tris": 0, "quads": 0, "ngons": 0, "quad_ratio": 0.0}


def test_shrinkwrap_kept_only_when_mean_distance_drops():
    # A snap that pulls verts closer to the proxy surface is kept...
    assert shrinkwrap_improves(0.50, 0.30) is True
    # ...one that made it worse, or did nothing, is discarded.
    assert shrinkwrap_improves(0.30, 0.50) is False
    assert shrinkwrap_improves(0.30, 0.30) is False


def test_shrinkwrap_requires_meaningful_improvement():
    # A change below the relative tolerance is treated as no gain (noise).
    assert shrinkwrap_improves(1.0, 1.0 - 1e-6) is False
    assert shrinkwrap_improves(1.0, 0.99) is True


def test_shrinkwrap_handles_degenerate_before_distance():
    assert shrinkwrap_improves(0.0, 0.0) is False


def test_attempt_to_dict_is_json_shaped():
    attempt = AdaptiveAttempt(
        label="adaptive_t5850",
        faces=5850, tris=5799, quads=51, ngons=0,
        non_manifold_edges=0, components=1,
        bbox_per_axis={"x": 0.99, "y": 0.99, "z": 0.985},
        bbox_min_ratio=0.985,
        proxy_to_low={"max_ratio": 0.02, "p99_ratio": 0.01},
        shrinkwrap_applied=True, wall_s=12.3,
    )
    d = attempt.to_dict()
    assert d["faces"] == 5850
    assert d["ngons"] == 0
    assert d["quad_ratio"] == round(51 / 5850, 4)
    assert d["bbox_min_ratio"] == 0.985
    assert d["shrinkwrap_applied"] is True


# -- A3 cleanup asserts (plan §6.2) -----------------------------------------


def _bd(faces, ngons=0):
    quads = faces - ngons
    return {"faces": faces, "tris": 0, "quads": quads, "ngons": ngons}


def test_cleanup_asserts_pass_on_clean_in_band_mesh():
    res = cleanup_asserts(_bd(5850), {"non_manifold_edges": 0, "components": 1},
                          target_face_count=5850)
    assert res["all_ok"] is True
    assert all(res[k] for k in ("ngons_ok", "non_manifold_ok", "components_ok", "band_ok"))


def test_cleanup_asserts_fail_on_ngons():
    res = cleanup_asserts(_bd(5850, ngons=3), {"non_manifold_edges": 0, "components": 1},
                          target_face_count=5850)
    assert res["ngons_ok"] is False
    assert res["all_ok"] is False


def test_cleanup_asserts_fail_on_non_manifold_and_extra_components():
    res = cleanup_asserts(_bd(5850), {"non_manifold_edges": 12, "components": 4},
                          target_face_count=5850)
    assert res["non_manifold_ok"] is False
    assert res["components_ok"] is False
    assert res["all_ok"] is False


def test_cleanup_asserts_fail_when_cleanup_moved_face_count_out_of_band():
    # tris->quads + dissolve can drift the count; >10% out of band must fail.
    res = cleanup_asserts(_bd(5000), {"non_manifold_edges": 0, "components": 1},
                          target_face_count=5850)
    assert res["band_ok"] is False
    assert res["all_ok"] is False


def test_cleanup_asserts_component_bound_is_configurable():
    res = cleanup_asserts(_bd(5850), {"non_manifold_edges": 0, "components": 2},
                          target_face_count=5850, component_bound=2)
    assert res["components_ok"] is True
    assert res["all_ok"] is True
