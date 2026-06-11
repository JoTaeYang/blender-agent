"""Progressive decimation retry ladder -- Phase DM5 (decimation plan §8).

Two layers, both offline:

- the **driver** (:func:`run_retry_ladder`) orchestration -- the plan §8 completion
  criteria: a per-attempt explanation, auto-escalation while the shape stays
  acceptable, and rollback to the last shape-accepted attempt when one breaks the
  shape -- exercised with scripted synthetic attempts;
- the concrete **pure-geometry strategies** (:func:`make_attempt_executor`) run on
  synthetic meshes, confirming each rung reduces and that the DM6 QEM rung is
  skipped until implemented.
"""

from retopo_agent.geometry.retry_ladder import (
    LADDER,
    METHOD_QEM,
    AttemptResult,
    AttemptSpec,
    make_attempt_executor,
    run_retry_ladder,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_grid_plane


# -- driver: scripted attempts --------------------------------------------


def _scripted(by_attempt):
    """execute() that returns a pre-scripted AttemptResult per attempt number, or
    None where the script has None (an unavailable rung)."""

    def execute(spec: AttemptSpec):
        spec_out = by_attempt.get(spec.attempt)
        if spec_out is None:
            return None
        actual, shape, band = spec_out
        return AttemptResult(spec.attempt, spec.method, 8008, actual, shape, band, note="scripted")

    return execute


def test_success_stops_at_target_accepted_shape_accepted():
    # Escalates; attempt 5 finally lands target accepted with good shape.
    script = {
        1: (8008, "accepted", "failed"),
        2: (7000, "accepted", "failed"),
        3: (5000, "accepted", "failed"),
        4: (3000, "accepted", "retry"),
        5: (2000, "accepted", "accepted"),
    }
    res = run_retry_ladder(_scripted(script), target_face_count=2000)
    assert res.selected_attempt == 5
    assert res.selection_reason == "target accepted and shape accepted"
    # Stopped at 5: attempt 6 (QEM) never ran.
    assert [a.attempt for a in res.attempts] == [1, 2, 3, 4, 5]


def test_warning_success_target_accepted_shape_retry():
    script = {1: (8008, "accepted", "failed"), 2: (2050, "retry", "accepted")}
    res = run_retry_ladder(_scripted(script), target_face_count=2000)
    assert res.selected_attempt == 2
    assert "warning success" in res.selection_reason


def test_rollback_to_last_shape_accepted_on_failure():
    # Attempts 1-3 keep shape acceptable but miss target; attempt 4 breaks shape.
    script = {
        1: (6000, "accepted", "failed"),
        2: (5000, "accepted", "failed"),
        3: (4000, "accepted", "failed"),  # closest shape-accepted to target 2000
        4: (2500, "failed", "retry"),  # shape broke chasing the target
    }
    res = run_retry_ladder(_scripted(script), target_face_count=2000)
    assert res.selected_attempt == 3  # rolled back to the last shape-accepted
    assert "rolled back" in res.selection_reason
    # Escalation stopped at the failure: attempt 5 never ran.
    assert [a.attempt for a in res.attempts] == [1, 2, 3, 4]


def test_rollback_with_no_prior_accepted_keeps_best_effort():
    script = {1: (2500, "failed", "retry")}
    res = run_retry_ladder(_scripted(script), target_face_count=2000)
    assert res.selected_attempt == 1
    assert "no prior shape-accepted" in res.selection_reason


def test_best_effort_when_target_never_reached():
    # Every attempt keeps shape accepted but none reaches the target band.
    script = {
        1: (6000, "accepted", "failed"),
        2: (5000, "accepted", "failed"),
        3: (4500, "accepted", "retry"),  # closest to 4000 target
        4: (3000, "accepted", "failed"),  # overshoot -> further from target again
        5: (3000, "accepted", "failed"),
    }
    res = run_retry_ladder(_scripted(script), target_face_count=4000)
    assert res.selected_attempt == 3  # closest-to-target shape-accepted attempt
    assert res.selection_reason.startswith("best effort")


def test_unavailable_rung_is_skipped():
    # Only attempts 1 and 6 scripted; 6 returns None (QEM not implemented).
    script = {1: (5000, "accepted", "failed"), 6: None}
    res = run_retry_ladder(_scripted(script), target_face_count=2000)
    assert [a.attempt for a in res.attempts] == [1]  # 6 skipped, 2-5 scripted None too
    assert res.selected_attempt == 1


def test_ladder_result_dict_contract():
    script = {1: (5000, "accepted", "failed"), 2: (2000, "accepted", "accepted")}
    d = run_retry_ladder(_scripted(script), target_face_count=2000).to_dict()
    for key in ("selected_attempt", "selection_reason", "target_face_count", "attempts"):
        assert key in d
    a0 = d["attempts"][0]
    for key in ("attempt", "method", "input_faces", "actual_faces", "shape_status", "target_band", "note"):
        assert key in a0


# -- concrete pure-geometry strategies ------------------------------------


def _multi_component(n_tiny=5):
    big = build_grid_plane(10, 10)  # 100 quads, one component
    verts = [v.co for v in big.vertices]
    faces = [list(f.vertex_ids) for f in big.faces]
    off = len(verts)
    for i in range(n_tiny):
        x = 5.0 + 2.0 * i
        verts += [(x, 0, 0), (x + 0.1, 0, 0), (x, 0.1, 0)]
        faces.append([off, off + 1, off + 2])
        off += 3
    return MeshGraph.from_faces("multi", verts, faces)


def test_executor_runs_ladder_and_skips_qem():
    base = _multi_component(5)
    execute = make_attempt_executor(base, 20, allow_component_removal=True)
    res = run_retry_ladder(execute, target_face_count=20)
    # Attempts 1-5 produced a candidate; the QEM rung (6) was skipped.
    assert all(a.method != METHOD_QEM for a in res.attempts)
    assert 1 <= len(res.attempts) <= 5
    # Every recorded attempt has a real candidate mesh and a valid status.
    for a in res.attempts:
        assert a.mesh is not None and a.actual_faces == a.mesh.face_count
        assert a.shape_status in {"accepted", "retry", "failed"}
        assert a.target_band in {"accepted", "retry", "failed"}
    assert res.selected_attempt is not None


def test_executor_qem_spec_returns_none():
    base = build_grid_plane(6, 6)
    execute = make_attempt_executor(base, 20)
    qem_spec = next(s for s in LADDER if s.method == METHOD_QEM)
    assert execute(qem_spec) is None


def test_component_strategy_removes_tiny_when_allowed():
    base = _multi_component(8)
    execute = make_attempt_executor(base, 30, allow_component_removal=True)
    component_spec = next(s for s in LADDER if s.params.get("strategy") == "component")
    res = execute(component_spec)
    assert res is not None
    assert "removed" in res.note  # tiny components dropped before collapse


def test_strategies_reduce_face_count():
    base = build_grid_plane(12, 12)  # 144 quads
    execute = make_attempt_executor(base, 16)
    for spec in LADDER:
        res = execute(spec)
        if res is None:
            continue
        assert res.actual_faces <= base.face_count
