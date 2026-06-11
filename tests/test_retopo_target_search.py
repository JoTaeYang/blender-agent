"""Target-face-count control loops (retopology plan §15.7).

These reproduce, offline, the failure that the anchor regression exposed -- a
remesh whose face count is far from target -- and prove the search loops drive it
into the acceptance band. The remesh operator is modelled by a deterministic
synthetic function so no Blender is required.
"""

import math

from retopo_agent.geometry.target_search import (
    ACCEPTED_ERROR,
    RETRY_ERROR,
    quality_band,
    search_quadriflow_target,
    search_voxel_size,
    target_error_ratio,
)


# -- bands -----------------------------------------------------------------


def test_quality_band_thresholds():
    # error 0.00 -> accepted, 0.20 -> retry, 0.50 -> failed (plan §15.6)
    assert quality_band(10000, 10000) == "accepted"
    assert quality_band(11500, 10000) == "accepted"  # exactly 0.15
    assert quality_band(13000, 10000) == "retry"  # 0.30 boundary
    assert quality_band(2774, 10000) == "failed"  # the anchor regression
    assert math.isclose(target_error_ratio(2774, 10000), 0.7226, rel_tol=1e-3)
    assert ACCEPTED_ERROR < RETRY_ERROR


# -- voxel size search -----------------------------------------------------


def _voxel_model(area: float):
    """Synthetic voxel remesh: face count ~ surface_area / voxel^2, the same
    inverse-square law the real operator approximately follows."""

    def measure(voxel: float) -> int:
        return int(round(area / (voxel * voxel)))

    return measure


def test_voxel_search_hits_target_from_coarse_start():
    # area chosen so the anchor-like coarse seed badly undershoots.
    area = 40000.0
    target = 10000
    measure = _voxel_model(area)
    # Anchor's failing single shot: voxel ~2.0 -> 10000 is the true answer; start
    # far too coarse (voxel 3.924 -> ~2597 faces, the regression).
    res = search_voxel_size(measure, target, initial=3.924, min_voxel=0.05, max_voxel=200.0)
    assert res.band == "accepted"
    assert res.error_ratio <= ACCEPTED_ERROR
    assert res.iterations <= 6


def test_voxel_search_converges_when_starting_too_dense():
    area = 250000.0
    target = 5000
    res = search_voxel_size(_voxel_model(area), target, initial=0.5, min_voxel=0.01, max_voxel=500.0)
    assert res.band == "accepted"
    # true voxel = sqrt(area/target) = sqrt(50) ~ 7.07
    assert math.isclose(res.value, math.sqrt(area / target), rel_tol=0.10)


def test_voxel_search_records_history_and_best():
    res = search_voxel_size(_voxel_model(40000.0), 10000, initial=20.0, min_voxel=0.05, max_voxel=200.0)
    assert len(res.history) == res.iterations
    # the returned face_count is the closest one visited
    best_visited = min(res.history, key=lambda vf: abs(vf[1] - 10000))
    assert res.face_count == best_visited[1]


def test_voxel_search_best_effort_when_target_unreachable():
    # Bracket forbids a small enough voxel to ever reach the target.
    res = search_voxel_size(_voxel_model(40000.0), 1_000_000, initial=2.0, min_voxel=1.0, max_voxel=5.0)
    assert res.face_count > 0  # still returns the densest (closest) it could reach
    assert res.band in {"retry", "failed"}


# -- QuadriFlow target search ----------------------------------------------


def _quadriflow_model(factor: float):
    """QuadriFlow tends to deliver ``factor * requested`` faces (e.g. 0.6x)."""

    def remesh(requested: int) -> int:
        return int(round(factor * requested))

    return remesh


def test_quadriflow_search_corrects_systematic_undershoot():
    target = 10000
    res = search_quadriflow_target(_quadriflow_model(0.6), target, max_iter=3)
    assert res.band == "accepted"
    assert res.error_ratio <= ACCEPTED_ERROR


def test_quadriflow_search_stops_on_hard_failure():
    def always_fail(requested: int) -> int:
        return 0

    res = search_quadriflow_target(always_fail, 10000, max_iter=3)
    assert res.face_count == 0  # signals the caller to fall back to voxel/cluster
    assert res.iterations == 1


def test_quadriflow_search_single_pass_when_already_on_target():
    res = search_quadriflow_target(_quadriflow_model(1.0), 8000, max_iter=3)
    assert res.iterations == 1
    assert res.band == "accepted"
