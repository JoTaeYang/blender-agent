"""Target-face-count control loops for Phase 1 generation (retopology plan §15.7).

The Phase 1 generators (QuadriFlow / voxel remesh) do *not* hit a face-count
target on the first try: QuadriFlow's ``target_faces`` is only a hint, and a
voxel remesh's face count depends on a voxel *size* whose relationship to face
count is mesh-dependent. Running either once and accepting the result is what
produced the anchor regression (``target 10000 -> actual 2774``).

This module is the fix the spec calls for (§15.7 "the actual resulting face
count should be checked, and binary search/retry should be used"). It is pure
Python -- the search loops take a ``measure`` callable and know nothing about
Blender -- so the control logic is unit-tested offline and reused by the Blender
adapter with the callable wired to the real operators.

Key relationships exploited:

- **Voxel remesh**: face count ~ surface_area / voxel_size^2, i.e. monotonically
  *decreasing* in voxel size. ``actual < target`` -> voxel too large -> shrink it.
- **QuadriFlow**: actual face count is roughly proportional to ``target_faces``,
  so a request scaled by ``target/actual`` corrects the next attempt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

# §15.6 acceptance bands, expressed as target_error_ratio = |actual-target|/target.
ACCEPTED_ERROR = 0.15
RETRY_ERROR = 0.30

# DM1 ``stopped_reason`` values for :func:`search_decimate_ratio` (plan §4).
STOP_ACCEPTED_BAND = "accepted_band"  # reached the tol_ratio acceptance band
STOP_HARD_FAILURE = "decimate_collapse_failed"  # a collapse yielded 0 faces
STOP_DECIMATE_PLATEAU = "decimate_collapse_plateau"  # ratio fell but faces did not
STOP_HIT_MIN_RATIO = "hit_min_ratio"  # ratio clamped to min_ratio with target unmet
STOP_CONVERGED = "converged"  # proportional guess stopped moving (clamped to a bound)
STOP_MAX_ITER = "max_iter"  # exhausted max_iter without converging
STOP_EMPTY_INPUT = "empty_input"  # no source/target faces to search over


def target_error_ratio(actual_face_count: int, target_face_count: int) -> float:
    if target_face_count <= 0:
        return 0.0
    return abs(actual_face_count - target_face_count) / target_face_count


def quality_band(actual_face_count: int, target_face_count: int) -> str:
    """``accepted`` / ``retry`` / ``failed`` per the §15.6 target-error bands."""
    err = target_error_ratio(actual_face_count, target_face_count)
    if err <= ACCEPTED_ERROR:
        return "accepted"
    if err <= RETRY_ERROR:
        return "retry"
    return "failed"


@dataclass
class SearchResult:
    """Outcome of a target-count search. ``value`` is the control parameter that
    produced the best result (a voxel size, or a QuadriFlow ``target_faces``).

    The ``stopped_reason`` / ``plateau_*`` / ``hit_min_ratio`` fields are the
    Decimation plan DM1 plateau-detection metadata: they explain *why* the search
    stopped so a ``failed`` band can be attributed to a Collapse topology floor
    rather than left unexplained (plan §4). They stay at their defaults for the
    voxel / QuadriFlow searches, which don't have a plateau concept.
    """

    value: float
    face_count: int
    iterations: int
    target_face_count: int
    history: list[tuple[float, int]] = field(default_factory=list)
    stopped_reason: str = ""
    plateau_face_count: int | None = None
    plateau_ratio: float | None = None
    hit_min_ratio: bool = False

    @property
    def error_ratio(self) -> float:
        return target_error_ratio(self.face_count, self.target_face_count)

    @property
    def band(self) -> str:
        return quality_band(self.face_count, self.target_face_count)

    @property
    def is_plateau(self) -> bool:
        return self.stopped_reason == STOP_DECIMATE_PLATEAU


def search_voxel_size(
    measure: Callable[[float], int],
    target_face_count: int,
    *,
    initial: float,
    min_voxel: float,
    max_voxel: float,
    max_iter: int = 6,
    tol_ratio: float = ACCEPTED_ERROR,
) -> SearchResult:
    """Find a voxel size whose remesh lands near ``target_face_count``.

    ``measure(voxel)`` returns the face count of a fresh voxel remesh at that
    size (``0`` signals failure). Because face count decreases monotonically with
    voxel size, we keep a ``[lo, hi]`` voxel bracket and step with a proportional
    guess (``voxel * sqrt(actual/target)`` inverts the area/voxel^2 law), falling
    back to geometric bisection when the guess leaves the bracket. Returns the
    best size visited (closest face count to target), even if ``tol_ratio`` is
    never met -- the caller decides whether the band is good enough.
    """
    if min_voxel <= 0 or max_voxel < min_voxel:
        raise ValueError("require 0 < min_voxel <= max_voxel")

    lo, hi = min_voxel, max_voxel  # voxel bracket; face count is decreasing in voxel
    voxel = min(max(initial, lo), hi)
    history: list[tuple[float, int]] = []
    best: tuple[float, int] | None = None

    for _ in range(max_iter):
        faces = max(0, int(measure(voxel)))
        history.append((voxel, faces))
        if faces > 0 and (best is None or target_error_ratio(faces, target_face_count) < target_error_ratio(best[1], target_face_count)):
            best = (voxel, faces)
        if faces > 0 and target_error_ratio(faces, target_face_count) <= tol_ratio:
            break

        # Narrow the bracket from the side we now know is wrong.
        if faces == 0 or faces < target_face_count:
            hi = min(hi, voxel)  # too sparse -> need a smaller (denser) voxel
        else:
            lo = max(lo, voxel)  # too dense -> need a larger (coarser) voxel

        if faces > 0:
            nxt = voxel * math.sqrt(faces / target_face_count)
        else:
            nxt = voxel * 0.5  # failed / empty: try markedly denser
        if not math.isfinite(nxt) or not (lo < nxt < hi):
            nxt = math.sqrt(lo * hi)  # geometric bisection within the bracket
        if abs(nxt - voxel) <= 1e-9 * max(voxel, 1.0):
            break
        voxel = min(max(nxt, lo), hi)

    if best is None:  # every attempt failed
        last_voxel, last_faces = history[-1] if history else (voxel, 0)
        return SearchResult(last_voxel, last_faces, len(history), target_face_count, history)
    return SearchResult(best[0], best[1], len(history), target_face_count, history)


def search_decimate_ratio(
    measure: Callable[[float], int],
    target_face_count: int,
    source_face_count: int,
    *,
    max_iter: int = 8,
    tol_ratio: float = ACCEPTED_ERROR,
    min_ratio: float = 1e-4,
    plateau_tol: float = 0.005,
    plateau_repeats: int = 2,
) -> SearchResult:
    """Find a Decimate-Collapse ``ratio`` whose result lands near the target.

    The Decimate (Collapse) modifier keeps roughly ``ratio`` of the source faces,
    so the first guess is ``target / source`` and each retry rescales by
    ``target / actual`` (the same proportional correction the QuadriFlow loop
    uses). ``ratio`` is clamped to ``(min_ratio, 1.0]``. A collapse that yields
    ``0`` faces is a hard failure and stops the search, mirroring
    :func:`search_quadriflow_target`. Returns the best ratio visited even when
    ``tol_ratio`` is never met -- the caller decides whether the band suffices.

    ``SearchResult.value`` is the chosen ratio in ``(0, 1]``.

    DM1 plateau detection (plan §4): the Collapse modifier hits a topology floor
    on meshes with non-manifold / boundary / detached geometry -- lowering the
    ratio stops lowering the face count (the anchor case floors at 8008 faces all
    the way down to ratio 0). When the ratio falls but the face count holds within
    ``plateau_tol`` (relative) for ``plateau_repeats`` consecutive measurements,
    the search stops early and records ``stopped_reason=decimate_collapse_plateau``
    with ``plateau_face_count`` / ``plateau_ratio`` so a missed target is
    explained as a modifier floor rather than left unattributed. ``hit_min_ratio``
    is recorded separately so a ``min_ratio`` clamp is not mistaken for a plateau.
    """
    if source_face_count <= 0 or target_face_count <= 0:
        return SearchResult(
            1.0, max(0, source_face_count), 0, target_face_count, [],
            stopped_reason=STOP_EMPTY_INPUT,
        )

    ratio = min(1.0, max(min_ratio, target_face_count / source_face_count))
    history: list[tuple[float, int]] = []
    best: tuple[float, int] | None = None
    stopped_reason = STOP_MAX_ITER
    plateau_face_count: int | None = None
    plateau_ratio: float | None = None
    hit_min_ratio = False
    same_run = 1  # consecutive measurements whose face count held while ratio fell

    for _ in range(max_iter):
        faces = max(0, int(measure(ratio)))
        history.append((ratio, faces))
        if faces <= 0:
            stopped_reason = STOP_HARD_FAILURE
            break  # hard failure -> let the caller fall back
        if best is None or target_error_ratio(faces, target_face_count) < target_error_ratio(best[1], target_face_count):
            best = (ratio, faces)
        if target_error_ratio(faces, target_face_count) <= tol_ratio:
            stopped_reason = STOP_ACCEPTED_BAND
            break

        # Plateau: a *lower* ratio than last time that barely moved the face count
        # means the Collapse modifier is stuck at a topology floor. Counted over
        # consecutive measurements so a single noisy step doesn't trip it.
        if len(history) >= 2:
            prev_ratio, prev_faces = history[-2]
            ratio_fell = ratio < prev_ratio - 1e-12
            faces_held = abs(faces - prev_faces) <= max(1, round(plateau_tol * prev_faces))
            same_run = same_run + 1 if (ratio_fell and faces_held) else 1
            if same_run >= plateau_repeats:
                stopped_reason = STOP_DECIMATE_PLATEAU
                plateau_face_count = faces
                plateau_ratio = ratio
                break

        nxt = min(1.0, max(min_ratio, ratio * target_face_count / faces))
        if nxt <= min_ratio + 1e-12:
            hit_min_ratio = True  # the next guess wanted to go below the floor ratio
        if abs(nxt - ratio) <= 1e-9:
            # Converged / clamped against a bound. If that bound is min_ratio with
            # the target still unmet, attribute it to the floor ratio specifically.
            stopped_reason = STOP_HIT_MIN_RATIO if hit_min_ratio else STOP_CONVERGED
            break
        ratio = nxt

    metadata = dict(
        stopped_reason=stopped_reason,
        plateau_face_count=plateau_face_count,
        plateau_ratio=plateau_ratio,
        hit_min_ratio=hit_min_ratio,
    )
    if best is None:
        last_ratio, last_faces = history[-1] if history else (ratio, 0)
        return SearchResult(last_ratio, last_faces, len(history), target_face_count, history, **metadata)
    return SearchResult(best[0], best[1], len(history), target_face_count, history, **metadata)


def search_quadriflow_target(
    remesh: Callable[[int], int],
    target_face_count: int,
    *,
    max_iter: int = 3,
    tol_ratio: float = ACCEPTED_ERROR,
    min_request: int = 8,
) -> SearchResult:
    """Adjust QuadriFlow's ``target_faces`` request until the actual count lands
    near ``target_face_count``.

    ``remesh(requested)`` runs a fresh QuadriFlow with that request and returns
    the actual face count (``0`` = failed / produced no usable change). Since the
    actual count tracks the request roughly linearly, the next request is scaled
    by ``target/actual``. A ``0`` result is treated as a hard failure and stops
    the search (retrying QuadriFlow rarely recovers from that).
    """
    requested = max(min_request, int(target_face_count))
    history: list[tuple[float, int]] = []
    best: tuple[int, int] | None = None

    for _ in range(max_iter):
        actual = max(0, int(remesh(requested)))
        history.append((requested, actual))
        if actual <= 0:
            break  # hard failure -> let the caller fall back
        if best is None or target_error_ratio(actual, target_face_count) < target_error_ratio(best[1], target_face_count):
            best = (requested, actual)
        if target_error_ratio(actual, target_face_count) <= tol_ratio:
            break
        nxt = max(min_request, int(round(requested * target_face_count / actual)))
        if nxt == requested:
            break  # converged / stuck
        requested = nxt

    if best is None:
        return SearchResult(requested, 0, len(history), target_face_count, history)
    return SearchResult(best[0], best[1], len(history), target_face_count, history)
