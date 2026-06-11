"""Decimation Optimize mode -- Phase D1 (decimation plan §7).

These exercise the Blender-free core of the Decimate-Collapse generator: the
``ratio`` control loop that drives the modifier to a target face count, plus the
worker's mode plumbing. The real ``bpy`` Decimate modifier is modelled by a
deterministic synthetic function (face count ~ source * ratio), so no Blender is
required -- the same offline pattern used for the QuadriFlow / voxel searches.
"""

import importlib.util
import os

from retopo_agent.geometry.decimate import decimate_to_target
from retopo_agent.geometry.shape_eval import (
    DECIMATION_SHAPE_THRESHOLDS,
    DEFAULT_SHAPE_THRESHOLDS,
    build_shape_report,
    evaluate_shape_match,
)
from retopo_agent.geometry.feature_compare import (
    FEATURE_OFF,
    FEATURE_ON,
    compare_feature_preservation,
)
from retopo_agent.geometry.target_search import (
    ACCEPTED_ERROR,
    STOP_ACCEPTED_BAND,
    STOP_DECIMATE_PLATEAU,
    STOP_HARD_FAILURE,
    STOP_HIT_MIN_RATIO,
    search_decimate_ratio,
)
from retopo_agent.io.fixtures import build_subdivided_cube, build_uv_sphere

# Decimation plan §7 Phase D1 completion criterion: anchor.obj reduced to 2,000.
ANCHOR_FACES = 9_828_864


def _collapse_model(source: int):
    """Synthetic Decimate (Collapse): keeps ~``ratio`` of the source faces, the
    near-linear relationship the real modifier approximately follows."""

    def measure(ratio: float) -> int:
        return max(0, int(round(source * ratio)))

    return measure


def test_decimate_ratio_hits_anchor_target_in_accepted_band():
    # Phase D1 completion: anchor.obj (9.83M faces) -> 2,000 must land accepted.
    target = 2000
    res = search_decimate_ratio(_collapse_model(ANCHOR_FACES), target, ANCHOR_FACES)
    assert res.band == "accepted"
    assert res.error_ratio <= ACCEPTED_ERROR
    # First guess target/source is already exact for a linear model.
    assert res.iterations <= 2


def test_decimate_ratio_corrects_systematic_offset():
    # A modifier that overshoots (keeps 1.4x the requested fraction) is corrected
    # by the target/actual rescale within a couple of iterations.
    source = 500_000

    def biased(ratio: float) -> int:
        return max(0, int(round(source * ratio * 1.4)))

    res = search_decimate_ratio(biased, 10_000, source)
    assert res.band == "accepted"
    assert 0.0 < res.value <= 1.0


def test_decimate_ratio_stops_on_hard_failure():
    res = search_decimate_ratio(lambda r: 0, 2000, 1_000_000)
    assert res.face_count == 0  # signals the caller a fallback is needed
    assert res.iterations == 1
    assert res.stopped_reason == STOP_HARD_FAILURE


# -- DM1 plateau detection and reporting (decimation plan §4) ----------------


def _plateau_collapse_model(source: int, floor_faces: int):
    """Synthetic Collapse that cannot go below ``floor_faces`` no matter how small
    the ratio -- the topology floor the anchor mesh hits at 8008 faces (plan §1)."""

    def measure(ratio: float) -> int:
        return max(floor_faces, int(round(source * ratio)))

    return measure


def test_decimate_ratio_detects_collapse_plateau():
    # DM1 completion criterion: anchor.obj -> 2000 floors at 8008 and the search
    # must explain itself as a Collapse plateau rather than a bare failure.
    res = search_decimate_ratio(_plateau_collapse_model(ANCHOR_FACES, 8008), 2000, ANCHOR_FACES)
    assert res.stopped_reason == STOP_DECIMATE_PLATEAU
    assert res.is_plateau
    assert res.plateau_face_count == 8008
    assert res.plateau_ratio is not None and res.plateau_ratio > 0.0
    assert res.face_count == 8008  # best (only) result visited
    assert res.band == "failed"  # 8008 vs 2000 is far outside the band


def test_decimate_ratio_plateau_tolerates_near_identical_counts():
    # "거의 동일" -- counts that wobble within plateau_tol still count as a plateau.
    def jittery(ratio: float) -> int:
        # Floors near 8000 with a small wobble (8005 -> 8000) as the ratio shrinks;
        # the 5-face gap is well within plateau_tol (0.5% of 8000 = 40 faces).
        base = max(8000, int(round(9_000_000 * ratio)))
        return base + (5 if ratio > 1.5e-4 else 0)

    res = search_decimate_ratio(jittery, 2000, 9_000_000)
    assert res.stopped_reason == STOP_DECIMATE_PLATEAU
    assert res.plateau_face_count in {8000, 8005}


def test_decimate_ratio_min_ratio_floor_is_not_a_plateau():
    # Face count keeps dropping but the ratio bottoms out before reaching target:
    # that is a min_ratio clamp, distinct from a Collapse plateau (plan §4).
    source = 1_000_000

    def measure(ratio: float) -> int:
        return max(0, int(round(source * ratio)))

    res = search_decimate_ratio(measure, 5, source, min_ratio=1e-3)
    assert res.hit_min_ratio is True
    assert res.stopped_reason == STOP_HIT_MIN_RATIO
    assert res.is_plateau is False
    assert res.plateau_face_count is None


def test_decimate_ratio_accepted_band_reports_clean_stop():
    res = search_decimate_ratio(_collapse_model(ANCHOR_FACES), 2000, ANCHOR_FACES)
    assert res.stopped_reason == STOP_ACCEPTED_BAND
    assert res.plateau_face_count is None
    assert res.hit_min_ratio is False


def test_decimate_ratio_clamped_to_unit_interval():
    # Target above the source can't be reached by reduction; ratio stays <= 1.
    source = 1000
    res = search_decimate_ratio(_collapse_model(source), source * 5, source)
    assert res.value <= 1.0
    assert res.face_count <= source


def test_decimate_ratio_zero_source_is_safe():
    res = search_decimate_ratio(_collapse_model(0), 2000, 0)
    assert res.iterations == 0
    assert res.value == 1.0


# -- worker mode plumbing (Ticket D1) --------------------------------------


def _load_worker():
    path = os.path.join(os.path.dirname(__file__), "..", "worker", "run_retopo_job.py")
    spec = importlib.util.spec_from_file_location("run_retopo_job", os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # top level imports no bpy (that is lazy in main())
    return mod


def test_decimation_mock_plan_shape():
    worker = _load_worker()
    plan = worker._decimation_mock_plan({"target_face_count": 2000})
    assert plan["mode"] == "decimation_optimize"
    assert plan["intent"] == "optimize_highpoly_decimation"
    assert plan["target_face_count"] == 2000
    assert plan["triangle_allowed"] is True
    assert plan["ngon_allowed"] is False
    tools = [step["tool"] for step in plan["plan"]]
    assert "generate_decimated_mesh" in tools


def test_decimation_mock_plan_defaults_target():
    worker = _load_worker()
    plan = worker._decimation_mock_plan({})
    assert plan["target_face_count"] == 2000  # Phase D1 default


# -- shape preservation evaluation (Phase D2, decimation plan §6.5) ---------


def test_decimation_thresholds_tolerate_more_normal_deviation():
    # 16 deg normal deviation: a clean triangle LOD under decimation bands
    # (accepted <= 20), but only "retry" under quad-retopo bands (accepted <= 12).
    kw = dict(bbox_diagonal=1.0, distances=[0.002] * 10, volume_error_ratio=None)
    quad = build_shape_report(normal_angles_deg=[16.0], thresholds=DEFAULT_SHAPE_THRESHOLDS, **kw)
    deci = build_shape_report(normal_angles_deg=[16.0], thresholds=DECIMATION_SHAPE_THRESHOLDS, **kw)
    assert quad.status == "retry"
    assert deci.status == "accepted"


def test_decimation_shape_report_emits_failure_reason():
    # Completion criterion: a failing/retrying result must explain itself.
    rep = build_shape_report(
        bbox_diagonal=1.0,
        distances=[0.08] * 10,  # max ratio 0.08 -> retry under both bands
        normal_angles_deg=[50.0],  # 50 deg -> failed even under decimation (retry 40)
        volume_error_ratio=None,
        thresholds=DECIMATION_SHAPE_THRESHOLDS,
    )
    assert rep.status == "failed"
    assert any("normal_deviation_mean_deg" in r for r in rep.reasons)
    assert rep.reasons  # never silent about why it left the accepted band


def test_decimated_sphere_shape_report_under_decimation_bands():
    high = build_uv_sphere(segments=48, rings=32)
    low = decimate_to_target(high, 600).low_mesh
    rep = evaluate_shape_match(high, low, thresholds=DECIMATION_SHAPE_THRESHOLDS)
    assert rep.sample_count > 0
    assert rep.status in {"accepted", "retry"}
    assert rep.to_dict()["status"] == rep.status  # serializable for shape_report.json


# -- feature-aware decimation comparison (Phase D3, decimation plan §7) ------


def test_feature_comparison_off_vs_on_same_target():
    # Phase D3 completion: compare preserve off/on at the SAME target face count.
    high = build_subdivided_cube(divisions=12)  # 864 faces; the 8 corners are features
    cmp = compare_feature_preservation(high, 150, feature_angle=30.0)

    assert cmp.off.target_face_count == cmp.on.target_face_count == 150
    assert cmp.feature_vertex_count > 0
    assert cmp.off.label == FEATURE_OFF and cmp.on.label == FEATURE_ON
    # Preserving the cube's hard edges keeps the worst-case deviation lower:
    # uniform clustering rounds the corners, feature-aware keeps them crisp.
    assert cmp.on.shape.surface_distance_max_ratio < cmp.off.shape.surface_distance_max_ratio
    assert cmp.preserves_shape_better
    assert cmp.surface_distance_max_ratio_improvement > 0.0


def test_feature_comparison_dict_is_serializable():
    high = build_subdivided_cube(divisions=10)
    cmp = compare_feature_preservation(high, 120, feature_angle=30.0).to_dict()
    assert cmp["comparison"] == "feature_preservation"
    assert cmp["target_face_count"] == 120
    for side in ("off", "on"):
        for key in ("label", "method", "actual_face_count", "shape_status", "surface_distance_max_ratio"):
            assert key in cmp[side]
    assert "surface_distance_max_ratio_improvement" in cmp
    assert isinstance(cmp["preserves_shape_better"], bool)


def test_feature_comparison_runs_on_curved_mesh():
    # The comparison must produce a valid both-sides report on a curved mesh too,
    # not just a hard-surface block (both variants land near the same target).
    high = build_uv_sphere(segments=40, rings=28)
    cmp = compare_feature_preservation(high, 400, feature_angle=30.0)
    assert cmp.off.shape.sample_count > 0 and cmp.on.shape.sample_count > 0
    assert cmp.off.shape.status in {"accepted", "retry", "failed"}
    assert cmp.on.shape.status in {"accepted", "retry", "failed"}
    assert cmp.off.actual_face_count < high.face_count
    assert cmp.on.actual_face_count < high.face_count
