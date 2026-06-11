"""Pure (Blender-free) tests for the P1 proxy planner (quad-retopo plan §7, §13).

The Blender-bound parts of :mod:`retopo_agent.blender.proxy` (import, voxel remesh,
BVH fidelity) are exercised by the headless acceptance run; the pure pieces are the
voxel-size first-guess and the proxy face-count band, which decide whether the
search converges quickly and whether the result is judged on a well-resolved proxy.
"""

import math

from retopo_agent.blender.proxy import (
    PROXY_FACE_MAX,
    PROXY_FACE_MIN,
    estimate_initial_voxel_size,
)
from retopo_agent.geometry.target_search import quality_band, search_voxel_size


# -- initial voxel-size estimate ------------------------------------------------


def test_initial_voxel_matches_p0_spike():
    """The area/voxel² law, calibrated on the P0 spike: voxel 0.974 -> 383,362
    faces implies area ≈ 3.636e5. Asking for ~1M faces should return voxel ≈ 0.603
    (≈ diag/969), exactly the divisor the spike note predicted for a ~1M proxy."""
    area = 383_362 * (0.9738 ** 2)  # ≈ 3.636e5, from the spike report
    diag = 584.25  # humanstatue bbox diagonal (P0)
    voxel = estimate_initial_voxel_size(area, 1_000_000, diag)
    assert math.isclose(voxel, math.sqrt(area / 1_000_000), rel_tol=1e-9)
    assert 0.59 < voxel < 0.62
    assert 950 < diag / voxel < 1000  # the predicted divisor band


def test_initial_voxel_inverse_sqrt_in_target():
    # Twice the faces -> 1/sqrt(2) the voxel size (the inverse-square-law inverse).
    area, diag = 3.636e5, 584.25
    v1 = estimate_initial_voxel_size(area, 500_000, diag)
    v2 = estimate_initial_voxel_size(area, 1_000_000, diag)
    assert math.isclose(v1 / v2, math.sqrt(2.0), rel_tol=1e-6)


def test_initial_voxel_clamped_to_diag_band():
    diag = 600.0
    # Absurdly small target would want a huge voxel -> clamp to diag/100.
    assert estimate_initial_voxel_size(1e12, 1, diag) == diag / 100.0
    # Absurdly large target would want a tiny voxel -> clamp to diag/4000.
    assert estimate_initial_voxel_size(1.0, 10**12, diag) == diag / 4000.0


def test_initial_voxel_falls_back_without_area():
    diag = 600.0
    assert estimate_initial_voxel_size(0.0, 1_000_000, diag) == diag / 600.0
    # No area and no diagonal -> safe constant, never zero/NaN.
    v = estimate_initial_voxel_size(0.0, 1_000_000, 0.0)
    assert v > 0 and math.isfinite(v)


# -- proxy band + search convergence -------------------------------------------


def test_proxy_band_constants_are_sane():
    assert PROXY_FACE_MIN < PROXY_FACE_MAX
    assert PROXY_FACE_MIN <= 1_000_000 <= PROXY_FACE_MAX


def test_seeded_search_converges_in_few_probes():
    """With the spike-calibrated seed the voxel search should land in band almost
    immediately — proving P1 won't pay for many 24.9M-face remesh probes."""
    area = 3.636e5

    def measure(voxel: float) -> int:
        return int(area / (voxel * voxel))  # the modelled inverse-square law

    target = 1_000_000
    seed = estimate_initial_voxel_size(area, target, 584.25)
    result = search_voxel_size(
        measure, target, initial=seed, min_voxel=584.25 / 4000, max_voxel=584.25 / 100, max_iter=4
    )
    assert quality_band(result.face_count, target) == "accepted"
    assert result.iterations <= 2  # the seed is essentially exact
