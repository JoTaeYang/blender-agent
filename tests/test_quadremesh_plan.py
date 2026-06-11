"""Pure (Blender-free) tests for the P2 QuadriFlow candidate selection (plan §8).

The QuadriFlow operator and the bmesh asserts are Blender-bound (exercised by the
headless run), but the *selection policy* — which of several seed/target attempts
becomes the result — is pure dict logic and decides whether a pure-quad attempt is
ever preferred over a closer-but-triangulated one. Importing ``quadremesh`` does not
trigger ``bpy`` (all Blender calls are lazy inside functions)."""

from retopo_agent.blender.quadremesh import (
    COVERAGE_BBOX_MAX_RATIO,
    COVERAGE_BBOX_MIN_RATIO,
    COVERAGE_MAX_RATIO,
    COVERAGE_P99_RATIO,
    _better,
)


def test_coverage_thresholds_sane():
    # Calibrated from the P2 coverage sweep: 10k passes (max_r 0.0137, bbox 0.99),
    # 6k/2.9k fail (max_r 0.092/0.147, bbox 0.97/0.80). The explosion guard sits
    # just above 1.0 to reject preserve_sharp's 24x-bbox flyaway output.
    assert 0 < COVERAGE_BBOX_MIN_RATIO < 1.0
    assert 1.0 < COVERAGE_BBOX_MAX_RATIO < 1.1
    assert 0 < COVERAGE_P99_RATIO < COVERAGE_MAX_RATIO < 1.0
    # The sweep's numbers must land on the right side of these cutoffs.
    assert 0.0137 <= COVERAGE_MAX_RATIO and 0.092 > COVERAGE_MAX_RATIO   # 10k in, 6k out
    assert 0.99 <= COVERAGE_BBOX_MIN_RATIO and 0.969 < COVERAGE_BBOX_MIN_RATIO  # 10k in, 6k out


def _cand(passes: bool, err: float):
    return {"obj": object(), "requested": 2900, "seed": 0,
            "metrics": {"passes_asserts": passes, "target_error_ratio": err}}


def test_first_candidate_always_wins():
    assert _better(_cand(False, 0.9), None, 2900) is True


def test_passing_beats_failing_even_if_farther():
    # A pure-quad/manifold attempt that is farther from target still beats a
    # closer attempt that fails the hard asserts (correctness over polycount).
    passing_far = _cand(True, 0.30)
    failing_near = _cand(False, 0.01)
    assert _better(passing_far, failing_near, 2900) is True
    assert _better(failing_near, passing_far, 2900) is False


def test_among_passing_closer_to_target_wins():
    near = _cand(True, 0.05)
    far = _cand(True, 0.20)
    assert _better(near, far, 2900) is True
    assert _better(far, near, 2900) is False


def test_among_failing_closer_to_target_wins():
    # Even when nothing passes, keep the closest fallback for the P4 ladder.
    near = _cand(False, 0.05)
    far = _cand(False, 0.40)
    assert _better(near, far, 2900) is True
