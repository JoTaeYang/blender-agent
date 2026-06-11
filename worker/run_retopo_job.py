"""Headless Blender retopology worker (retopology plan §8, §10 Phase 1).

Run inside Blender (plan §15.12 CLI form):

    blender --background input.blend \
        --python worker/run_retopo_job.py -- \
        --provider mock \
        --object-name HighPolyObject \
        --target-face-count 10000 \
        --topology-level low_retopo \
        --quad-required true \
        --ngon-allowed false

Phase 1 scope: duplicate the selected high-poly object, generate a low-poly
candidate near the target face count (QuadriFlow -> voxel -> cluster-decimate
fallback ladder), project it back with Shrinkwrap, and create a separate result
object. Topology *validation* and shape *evaluation* (Phases 2-3) are not run
here yet.

``--mode decimation_optimize`` selects the sibling Decimation Optimize pipeline
(decimation plan §7) instead: a triangle-based Decimate (Collapse) reduction to a
target face count (ZBrush-Decimation-Master style), handled by
:func:`_run_decimation`. The default mode stays ``quad_retopo``.

Outputs written to ``out_dir``:

    retopo_plan.json        the (mock) deterministic plan that drove the job
    generation_report.json  what the generator actually produced
    lowpoly.blend           the scene with the result object (best-effort)
    lowpoly.fbx             the result object exported (best-effort)
"""

from __future__ import annotations

import json
import os
import sys


def _parse_args(argv: list[str]) -> dict:
    # Only consider args after the "--" separator (Blender convention).
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            key = argv[i][2:].replace("-", "_")
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[key] = argv[i + 1]  # "--target-face-count 10000"
                i += 2
            else:
                opts[key] = "true"  # bare flag, e.g. "--quad-required"
                i += 1
        else:
            i += 1
    return opts


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_list(value) -> list[str]:
    """Accept a JSON list or a comma-separated string (CLI) -> list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _ensure_importable() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def _mock_plan(job: dict) -> dict:
    """Deterministic rule-based plan (plan §15.12 ``MockRetopoPlanProvider``).

    Phase 1 only executes the ``generate_lowpoly_mesh`` step; the projection /
    validation / evaluation entries are recorded for forward compatibility with
    Phases 2-3 and the Phase 7 agent loop.
    """
    target = int(job.get("target_face_count", 10000))
    return {
        "intent": "create_lowpoly_retopology",
        "provider": job.get("provider", "mock"),
        "target_face_count": target,
        "topology_level": job.get("topology_level", "low_retopo"),
        "quad_required": _as_bool(job.get("quad_required"), True),
        "ngon_allowed": _as_bool(job.get("ngon_allowed"), False),
        "shape_preservation": job.get("shape_preservation", "high"),
        "feature_policy": {
            "preserve_silhouette": True,
            "preserve_hard_edges": _as_bool(job.get("preserve_hard_edges"), True),
            "small_details": "normal_bake",
        },
        "plan": [
            {
                "tool": "generate_lowpoly_mesh",
                "args": {"method": "quadriflow_remesh", "target_face_count": target},
            },
            {"tool": "project_lowpoly_to_highpoly", "args": {"method": "shrinkwrap"}},
        ],
    }


def _decimation_mock_plan(job: dict) -> dict:
    """Deterministic plan for Decimation Optimize mode (decimation plan §9).

    Phase D1 only executes the ``generate_decimated_mesh`` step; the projection /
    validation entries are recorded for forward compatibility with Phases D2-D4.
    """
    target = int(job.get("target_face_count", 2000))
    return {
        "intent": "optimize_highpoly_decimation",
        "mode": "decimation_optimize",
        "provider": job.get("provider", "mock"),
        "target_face_count": target,
        "triangle_allowed": True,
        "ngon_allowed": _as_bool(job.get("ngon_allowed"), False),
        "preserve_features": _as_bool(job.get("preserve_features"), False),
        "feature_angle": float(job.get("feature_angle", 30.0)),
        # DM4 importance map: graded vertex-group protection + strength (plan §7).
        "preserve_features_strength": float(job.get("preserve_features_strength", 1.0)),
        "use_importance_map": _as_bool(job.get("use_importance_map"), False),
        "apply_shrinkwrap": _as_bool(job.get("apply_shrinkwrap"), False),
        "transfer_normals": _as_bool(job.get("transfer_normals"), False),
        # DM3 component budget policy (resolved against the DM2 diagnosis at run time).
        "decimation_policy": job.get("decimation_policy", "balanced"),
        "component_policy": job.get("component_policy"),  # None -> use diagnosis recommendation
        "allow_component_removal": _as_bool(
            job.get("allow_component_removal"),
            str(job.get("decimation_policy", "balanced")).strip().lower() == "strict_target",
        ),
        # DM5 progressive retry ladder, run automatically when the primary collapse
        # misses the target band (plan §8). On by default.
        "retry_ladder": _as_bool(job.get("retry_ladder"), True),
        "plan": [
            {
                "tool": "generate_decimated_mesh",
                "args": {"method": "decimate_collapse", "target_face_count": target},
            },
        ],
    }


def main() -> int:
    _ensure_importable()

    import bpy  # only available inside Blender

    from retopo_agent.blender.features import analyze_features_blender
    from retopo_agent.blender.quadflow import improve_quad_flow_blender
    from retopo_agent.blender.retopo import generate_lowpoly_object
    from retopo_agent.blender.shape import evaluate_shape_match_blender, render_shape_preview
    from retopo_agent.geometry.quadflow import quad_flow_score
    from retopo_agent.geometry.validate import validate_topology
    from uv_agent.blender.extract import extract_mesh_graph

    opts = _parse_args(sys.argv)
    if "job" in opts:
        with open(opts["job"], "r", encoding="utf-8") as fh:
            job = json.load(fh)
    else:
        job = dict(opts)

    object_name = job.get("object_name")
    obj = bpy.data.objects.get(object_name) if object_name else None
    if obj is None or obj.type != "MESH":
        obj = next((o for o in bpy.data.objects if o.type == "MESH"), None)
    if obj is None:
        print("run_retopo_job: no mesh object found", file=sys.stderr)
        return 2

    out_dir = job.get("out_dir", os.path.join("out", str(job.get("job_id", "job"))))
    os.makedirs(out_dir, exist_ok=True)

    # Decimation Optimize mode is a separate branch from quad retopo (plan §10).
    mode = job.get("mode", "quad_retopo")
    if mode == "decimation_optimize":
        return _run_decimation(bpy, job, obj, out_dir)

    plan = _mock_plan(job)
    with open(os.path.join(out_dir, "retopo_plan.json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)

    # Phase 5: analyze the high-poly's shape-defining features (plan §6.1).
    feature_angle = float(job.get("feature_angle", 30.0))
    preserve_features = _as_bool(job.get("preserve_features"), False)
    voxel_adaptivity = float(job.get("voxel_adaptivity", 0.0))
    feature_report = analyze_features_blender(obj, feature_angle)
    with open(os.path.join(out_dir, "feature_report.json"), "w", encoding="utf-8") as fh:
        json.dump(feature_report, fh, indent=2)
    print(
        f"run_retopo_job: features hard_edge_ratio={feature_report['hard_edge_ratio']} "
        f"max_dihedral={feature_report['max_dihedral_deg']}deg "
        f"(preserve_features={preserve_features}, voxel_adaptivity={voxel_adaptivity})"
    )

    # Phase 4: batch mode -- multiple topology levels / targets from one object.
    levels = _parse_list(job.get("levels"))
    targets = [int(t) for t in _parse_list(job.get("targets"))]
    if levels or targets:
        return _run_batch(bpy, job, plan, obj, out_dir, levels, targets,
                          preserve_features, feature_angle, voxel_adaptivity)

    target = int(plan["target_face_count"])
    result = generate_lowpoly_object(
        obj,
        target,
        apply_shrinkwrap=_as_bool(job.get("apply_shrinkwrap"), True),
        preserve_sharp=plan["feature_policy"]["preserve_hard_edges"],
        preserve_features=preserve_features,
        feature_angle=feature_angle,
        voxel_adaptivity=voxel_adaptivity,
    )

    report = {
        "object_name": obj.name,
        "result_object_name": result.obj.name,
        "method": result.method,
        "source_face_count": result.source_face_count,
        "target_face_count": result.target_face_count,
        "actual_face_count": result.actual_face_count,
        "target_error_ratio": round(result.target_error_ratio, 4),
        "band": result.band,
        "notes": result.notes,
    }
    with open(os.path.join(out_dir, "generation_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(
        f"run_retopo_job: {result.method} {result.source_face_count} -> "
        f"{result.actual_face_count} faces (target {target}, band={result.band}) -> '{result.obj.name}'"
    )

    # Phase 6: quad-flow scoring (and optional improvement) (plan §6.6, §10 Phase 6).
    qf_before = quad_flow_score(extract_mesh_graph(result.obj))
    qf_report = {"improved": False, "before": qf_before.to_dict(), "after": qf_before.to_dict()}
    if _as_bool(job.get("improve_quad_flow"), False):
        notes = improve_quad_flow_blender(
            result.obj,
            smooth_iterations=int(job.get("quadflow_smooth_iterations", 5)),
            shrinkwrap_target=obj if _as_bool(job.get("apply_shrinkwrap"), True) else None,
        )
        qf_after = quad_flow_score(extract_mesh_graph(result.obj))
        qf_report = {"improved": True, "notes": notes, "before": qf_before.to_dict(), "after": qf_after.to_dict()}
        print(
            f"run_retopo_job: quad_flow_score {qf_before.score:.3f} -> {qf_after.score:.3f} "
            f"(quad_fraction {qf_before.quad_fraction:.3f} -> {qf_after.quad_fraction:.3f})"
        )
    else:
        print(f"run_retopo_job: quad_flow_score={qf_before.score:.3f} (improvement off)")
    with open(os.path.join(out_dir, "quadflow_report.json"), "w", encoding="utf-8") as fh:
        json.dump(qf_report, fh, indent=2)

    # Phase 2: validate the generated topology (plan §8.2 step 7, §6.6).
    low_graph = extract_mesh_graph(result.obj)
    validation = validate_topology(
        low_graph,
        target,
        quad_required=plan["quad_required"],
        ngon_allowed=plan["ngon_allowed"],
        expect_closed=_as_bool(job.get("expect_closed"), True),
    )
    with open(os.path.join(out_dir, "validation_report.json"), "w", encoding="utf-8") as fh:
        json.dump(validation.to_dict(), fh, indent=2)
    print(
        f"run_retopo_job: validation status={validation.status} "
        f"quad_ratio={validation.quad_ratio:.3f} tris={validation.triangle_count} "
        f"ngons={validation.ngon_count} non_manifold={validation.non_manifold_edge_count}"
    )

    # Phase 3: shape-preservation evaluation (plan §8.2 step 8, §6.7).
    # Must run while BOTH the high-poly source and the low-poly result exist.
    shape = evaluate_shape_match_blender(obj, result.obj)
    with open(os.path.join(out_dir, "shape_report.json"), "w", encoding="utf-8") as fh:
        json.dump(shape.to_dict(), fh, indent=2)
    print(
        f"run_retopo_job: shape status={shape.status} "
        f"mean_ratio={shape.surface_distance_mean_ratio:.4f} "
        f"max_ratio={shape.surface_distance_max_ratio:.4f} "
        f"normal_dev_deg={shape.normal_deviation_mean_deg:.2f}"
    )

    if _as_bool(job.get("render_preview"), False):
        render_shape_preview(result.obj, os.path.join(out_dir, "preview.png"))

    # Keep only the low-poly result in the saved scene (plan: export only the
    # low-poly). Removing the high-poly source also keeps lowpoly.blend small.
    if not _as_bool(job.get("keep_source"), False):
        _remove_object(bpy, obj)

    # Best-effort persistence of the result.
    try:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(os.path.join(out_dir, "lowpoly.blend")))
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        print(f"run_retopo_job: save .blend skipped ({exc})")
    try:
        for o in bpy.context.view_layer.objects:
            o.select_set(o is result.obj)
        bpy.context.view_layer.objects.active = result.obj
        bpy.ops.export_scene.fbx(
            filepath=os.path.abspath(os.path.join(out_dir, "lowpoly.fbx")),
            use_selection=True,
        )
    except Exception as exc:  # noqa: BLE001 - export is best-effort
        print(f"run_retopo_job: FBX export skipped ({exc})")

    print(f"run_retopo_job: done out_dir={out_dir}")
    return 0


def _run_decimation(bpy, job, obj, out_dir) -> int:
    """Decimation Optimize mode (plan §7 Phases D1-D3).

    Duplicates the high-poly, drives the Decimate (Collapse) ratio search to the
    target (D1), writes ``decimation_plan.json`` + ``generation_report.json``,
    evaluates shape preservation with the decimation-specific thresholds and
    writes ``shape_report.json`` (D2). Phase DM2: writes a pre-process topology
    ``decimation_diagnosis.json`` of the result (components, boundaries,
    degeneracy) and a recommended component policy for the retry ladder. Phase DM3:
    writes a per-component face-budget plan ``component_budget.json`` under the
    resolved component policy, with the with/without tiny-component-removal lower
    bounds. Phase DM4: writes an ``importance_map.json`` (per-vertex importance
    distribution + active sources); ``--use-importance-map`` drives the collapse
    vertex group with the graded map, and ``--preserve-features-strength`` sets the
    protection strength. Phase DM5: when the primary collapse misses the target
    band, a progressive retry ladder (feature-protected collapse -> cleanup ->
    planar reduction -> component-budget removal) escalates while shape stays
    acceptable, writes ``decimation_attempts.json``, and swaps in the selected
    candidate. Phase D3: a hard-edge/curvature
    ``feature_report.json`` is always written; ``--preserve-features`` weights the
    collapse to keep features; ``--compare-features`` generates a preserve-off vs
    preserve-on ``feature_comparison.json`` at the same target. Normal cleanup is
    a later phase (D4).
    """
    from retopo_agent.blender.cleanup import cleanup_decimated_normals
    from retopo_agent.blender.component_budget import plan_component_budget_blender
    from retopo_agent.blender.decimate import generate_decimated_object
    from retopo_agent.blender.diagnosis import diagnose_decimation_blender
    from retopo_agent.blender.features import analyze_features_blender
    from retopo_agent.blender.importance import compute_importance_map_blender, importance_vertex_weights
    from retopo_agent.blender.retry_ladder import run_decimation_retry_ladder_blender
    from retopo_agent.blender.shape import evaluate_shape_match_blender
    from retopo_agent.geometry.component_budget import normalize_policy
    from retopo_agent.geometry.shape_eval import DECIMATION_SHAPE_THRESHOLDS

    plan = _decimation_mock_plan(job)
    with open(os.path.join(out_dir, "decimation_plan.json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)

    target = int(plan["target_face_count"])
    preserve_features = _as_bool(job.get("preserve_features"), False)
    feature_angle = float(job.get("feature_angle", 30.0))
    triangulate = _as_bool(job.get("triangulate"), True)
    preserve_features_strength = float(plan["preserve_features_strength"])
    use_importance_map = bool(plan["use_importance_map"])

    # Phase D3: reuse the hard-edge/curvature feature report (sampled, scales).
    feature_report = analyze_features_blender(obj, feature_angle)
    with open(os.path.join(out_dir, "feature_report.json"), "w", encoding="utf-8") as fh:
        json.dump(feature_report, fh, indent=2)
    print(
        f"run_retopo_job: features hard_edge_ratio={feature_report['hard_edge_ratio']} "
        f"max_dihedral={feature_report['max_dihedral_deg']}deg "
        f"(preserve_features={preserve_features})"
    )

    # Phase D3: preserve-off vs preserve-on comparison at the same target (§7).
    if _as_bool(job.get("compare_features"), False):
        return _run_decimation_feature_comparison(
            bpy, obj, out_dir, target, feature_angle, triangulate,
            DECIMATION_SHAPE_THRESHOLDS, keep_source=_as_bool(job.get("keep_source"), False),
        )

    # DM4: graded importance-map weights drive the collapse when --use-importance-map
    # is set and the source is small enough for a full map; else fall back to the
    # binary feature group. Either way preserve_features_strength sets the strength.
    importance_weights = None
    if use_importance_map:
        importance_weights = importance_vertex_weights(
            obj, strength=preserve_features_strength, angle_threshold=feature_angle
        )
        if importance_weights is None:
            print("run_retopo_job: importance map skipped on source (too large); using feature group")
        else:
            print(f"run_retopo_job: importance map drives collapse ({len(importance_weights)} weighted verts)")

    result = generate_decimated_object(
        obj,
        target,
        triangulate=triangulate,
        preserve_features=preserve_features,
        feature_angle=feature_angle,
        preserve_features_strength=preserve_features_strength,
        importance_weights=importance_weights,
    )

    report = {
        "mode": "decimation_optimize",
        "object_name": obj.name,
        "result_object_name": result.obj.name,
        "method": result.method,
        "ratio": round(result.ratio, 6),
        "preserve_features": result.preserve_features,
        "feature_vertex_count": result.feature_vertex_count,
        # DM4 importance-map feature protection (decimation plan §7).
        "preserve_features_strength": result.preserve_features_strength,
        "importance_weighted": result.importance_weighted,
        "source_face_count": result.source_face_count,
        "target_face_count": result.target_face_count,
        "actual_face_count": result.actual_face_count,
        "target_error_ratio": round(result.target_error_ratio, 4),
        "band": result.band,
        # DM1 plateau detection / reporting (decimation plan §4).
        "stopped_reason": result.stopped_reason,
        "plateau_face_count": result.plateau_face_count,
        "plateau_ratio": round(result.plateau_ratio, 6) if result.plateau_ratio is not None else None,
        "hit_min_ratio": result.hit_min_ratio,
        "search_iterations": result.search_iterations,
        "search_history": [[round(r, 6), f] for r, f in result.search_history],
        "notes": result.notes,
    }
    print(
        f"run_retopo_job: decimate_collapse {result.source_face_count} -> "
        f"{result.actual_face_count} faces (target {target}, ratio={result.ratio:.4g}, "
        f"band={result.band}, stopped={result.stopped_reason}) -> '{result.obj.name}'"
    )
    if result.plateau_face_count is not None:
        print(
            f"run_retopo_job: Collapse plateau at {result.plateau_face_count} faces "
            f"(ratio={result.plateau_ratio:.4g}); target {target} not reachable by ratio alone"
        )

    # Phase DM2: pre-process topology diagnosis of the decimated result (plan §5).
    # The result -- e.g. the anchor's Collapse plateau -- is where the detached
    # component / non-manifold structure that blocks a lower target shows up, so its
    # recommended_policy is what the DM3 / DM5 retry would apply.
    diagnosis = diagnose_decimation_blender(result.obj)
    if diagnosis is not None:
        with open(os.path.join(out_dir, "decimation_diagnosis.json"), "w", encoding="utf-8") as fh:
            json.dump(diagnosis.to_dict(), fh, indent=2)
        report["recommended_policy"] = diagnosis.recommended_policy
        report["needs_cleanup"] = diagnosis.needs_cleanup
        print(
            f"run_retopo_job: diagnosis components={diagnosis.component_count} "
            f"(tiny={diagnosis.tiny_component_count}, largest_ratio={diagnosis.largest_component_face_ratio:.3f}) "
            f"boundary={diagnosis.boundary_edge_count} non_manifold={diagnosis.non_manifold_edge_count} "
            f"-> recommended_policy={diagnosis.recommended_policy}"
        )
        if diagnosis.needs_cleanup:
            print(f"run_retopo_job: diagnosis cleanup signals: {'; '.join(diagnosis.cleanup_reasons)}")
    else:
        report["recommended_policy"] = None
        print("run_retopo_job: diagnosis skipped (result too large for graph diagnosis)")

    # Phase DM3: component budget policy (plan §6). The policy is the explicit
    # --component-policy if given, else the DM2 diagnosis recommendation; removal
    # of tiny shells is off unless --allow-component-removal (strict_target).
    explicit_policy = plan.get("component_policy")
    component_policy = normalize_policy(
        explicit_policy if explicit_policy else (diagnosis.recommended_policy if diagnosis else None)
    )
    allow_removal = bool(plan.get("allow_component_removal", False))
    budget = plan_component_budget_blender(
        result.obj, target, policy=component_policy, allow_removal=allow_removal
    )
    if budget is not None:
        with open(os.path.join(out_dir, "component_budget.json"), "w", encoding="utf-8") as fh:
            json.dump(budget.to_dict(), fh, indent=2)
        report["component_policy"] = budget.policy
        report["allow_component_removal"] = allow_removal
        report["component_lower_bound_without_removal"] = budget.lower_bound_without_removal
        report["component_lower_bound_with_removal"] = budget.lower_bound_with_removal
        print(
            f"run_retopo_job: component budget policy={budget.policy} "
            f"components={budget.component_count} (tiny={budget.tiny_component_count}) "
            f"allocated={budget.allocated_total} lower_bound no_removal={budget.lower_bound_without_removal} "
            f"with_removal={budget.lower_bound_with_removal}"
        )
        if budget.removed_component_count:
            print(
                f"run_retopo_job: budget would remove {budget.removed_component_count} components "
                f"({budget.removed_face_count} faces) under allow_removal"
            )

    # Phase DM4: importance map of the result (plan §7). Reports the per-vertex
    # importance distribution and which sources fired; the same map can drive the
    # collapse vertex group via --use-importance-map (above).
    importance_map = compute_importance_map_blender(result.obj, angle_threshold=feature_angle)
    if importance_map is not None:
        with open(os.path.join(out_dir, "importance_map.json"), "w", encoding="utf-8") as fh:
            json.dump(importance_map.to_dict(), fh, indent=2)
        stats = importance_map.importance_stats
        active = [k for k, v in importance_map.sources.items() if v]
        report["importance_stats"] = stats
        report["importance_sources"] = active
        print(
            f"run_retopo_job: importance map mean={stats['mean']} max={stats['max']} "
            f"sources={','.join(active) if active else 'none'}"
        )

    # Phase DM5: progressive retry ladder (plan §8). When the primary collapse
    # misses the target band (e.g. a plateau), escalate through feature-protected
    # collapse / cleanup / planar reduction / component-budget removal on the
    # plateau result, keeping shape acceptable and rolling back if one breaks it.
    if bool(plan.get("retry_ladder", True)) and result.band != "accepted":
        ladder, retry_obj = run_decimation_retry_ladder_blender(
            result.obj, target, reference_obj=obj, feature_angle=feature_angle,
            allow_component_removal=allow_removal, shape_thresholds=DECIMATION_SHAPE_THRESHOLDS,
        )
        if ladder is not None:
            with open(os.path.join(out_dir, "decimation_attempts.json"), "w", encoding="utf-8") as fh:
                json.dump(ladder.to_dict(), fh, indent=2)
            report["retry_selected_attempt"] = ladder.selected_attempt
            report["retry_selection_reason"] = ladder.selection_reason
            print(
                f"run_retopo_job: retry ladder ran {len(ladder.attempts)} attempts -> "
                f"selected attempt {ladder.selected_attempt} ({ladder.selection_reason})"
            )
            for a in ladder.attempts:
                print(
                    f"run_retopo_job:   attempt {a.attempt} {a.method}: "
                    f"{a.input_faces} -> {a.actual_faces} faces, shape={a.shape_status}, "
                    f"target_band={a.target_band}"
                )
            if retry_obj is not None:
                old_obj = result.obj
                result.obj = retry_obj  # the selected candidate becomes the result
                _remove_object(bpy, old_obj)
                report["result_object_name"] = result.obj.name
                report["actual_face_count"] = result.actual_face_count
                report["target_error_ratio"] = round(result.target_error_ratio, 4)
                report["band"] = result.band
                print(
                    f"run_retopo_job: retry ladder improved result -> {result.actual_face_count} "
                    f"faces (band={result.band}), object '{result.obj.name}'"
                )

    # Phase D4: normal / visual cleanup (plan §6.4). Auto Smooth + Weighted Normal
    # (+ optional normal transfer from the still-present high-poly) reduce shading
    # artifacts on the triangle LOD.
    if _as_bool(job.get("normal_cleanup"), False):
        auto_smooth_angle = float(job.get("auto_smooth_angle", feature_angle))
        cleanup = cleanup_decimated_normals(
            result.obj,
            obj,
            auto_smooth_angle=auto_smooth_angle,
            weighted_normal=_as_bool(job.get("weighted_normal"), True),
            transfer_normals=_as_bool(job.get("transfer_normals"), False),
            triangulate=triangulate,
        )
        report["normal_cleanup"] = cleanup
        print(f"run_retopo_job: normal cleanup applied={cleanup['applied']}")

    with open(os.path.join(out_dir, "generation_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    # Phase D2: shape-preservation evaluation (plan §6.5). Must run while BOTH the
    # high-poly source and the low-poly result exist, with the decimation bands
    # (triangle LOD tolerates more normal deviation than quad retopo).
    shape = evaluate_shape_match_blender(obj, result.obj, thresholds=DECIMATION_SHAPE_THRESHOLDS)
    with open(os.path.join(out_dir, "shape_report.json"), "w", encoding="utf-8") as fh:
        json.dump(shape.to_dict(), fh, indent=2)
    print(
        f"run_retopo_job: shape status={shape.status} "
        f"mean_ratio={shape.surface_distance_mean_ratio:.4f} "
        f"max_ratio={shape.surface_distance_max_ratio:.4f} "
        f"normal_dev_deg={shape.normal_deviation_mean_deg:.2f}"
    )
    if shape.status != "accepted":
        print(f"run_retopo_job: shape {shape.status} reasons: {'; '.join(shape.reasons)}")

    # Export only the low-poly result (plan §8): drop the high-poly source first
    # so lowpoly.blend stays small.
    if not _as_bool(job.get("keep_source"), False):
        _remove_object(bpy, obj)
    _save_and_export(bpy, out_dir, [result.obj])

    print(f"run_retopo_job: decimation done out_dir={out_dir}")
    return 0


def _run_decimation_feature_comparison(bpy, obj, out_dir, target, feature_angle, triangulate, thresholds,
                                       *, keep_source=False) -> int:
    """Phase D3: generate the same target with feature preservation off and on and
    write ``feature_comparison.json`` (plan §7 completion criterion). Both result
    objects are kept in the saved scene so the difference is inspectable."""
    from retopo_agent.blender.decimate import generate_decimated_object
    from retopo_agent.blender.shape import evaluate_shape_match_blender
    from retopo_agent.geometry.feature_compare import FEATURE_OFF, FEATURE_ON

    variants = []
    objs = []
    for label, preserve in ((FEATURE_OFF, False), (FEATURE_ON, True)):
        result = generate_decimated_object(
            obj, target, triangulate=triangulate, preserve_features=preserve, feature_angle=feature_angle,
        )
        shape = evaluate_shape_match_blender(obj, result.obj, thresholds=thresholds)
        objs.append(result.obj)
        variants.append({
            "label": label,
            "method": result.method,
            "preserve_features": result.preserve_features,
            "feature_vertex_count": result.feature_vertex_count,
            "target_face_count": result.target_face_count,
            "actual_face_count": result.actual_face_count,
            "target_error_ratio": round(result.target_error_ratio, 4),
            "stopped_reason": result.stopped_reason,
            "plateau_face_count": result.plateau_face_count,
            "shape_status": shape.status,
            "surface_distance_mean_ratio": round(shape.surface_distance_mean_ratio, 5),
            "surface_distance_max_ratio": round(shape.surface_distance_max_ratio, 5),
            "normal_deviation_mean_deg": round(shape.normal_deviation_mean_deg, 3),
        })
        print(
            f"run_retopo_job: {label} -> {result.actual_face_count} faces, "
            f"shape={shape.status} max_ratio={shape.surface_distance_max_ratio:.4f}"
        )

    off, on = variants[0], variants[1]
    comparison = {
        "comparison": "feature_preservation",
        "target_face_count": target,
        "feature_angle_deg": feature_angle,
        "off": off,
        "on": on,
        "surface_distance_max_ratio_improvement": round(
            off["surface_distance_max_ratio"] - on["surface_distance_max_ratio"], 5
        ),
        "surface_distance_mean_ratio_improvement": round(
            off["surface_distance_mean_ratio"] - on["surface_distance_mean_ratio"], 5
        ),
        "preserves_shape_better": on["surface_distance_max_ratio"] <= off["surface_distance_max_ratio"],
    }
    with open(os.path.join(out_dir, "feature_comparison.json"), "w", encoding="utf-8") as fh:
        json.dump(comparison, fh, indent=2)
    print(
        f"run_retopo_job: feature comparison max_ratio off={off['surface_distance_max_ratio']:.4f} "
        f"on={on['surface_distance_max_ratio']:.4f} "
        f"(improvement={comparison['surface_distance_max_ratio_improvement']:.4f})"
    )

    if not keep_source:
        _remove_object(bpy, obj)
    _save_and_export(bpy, out_dir, objs, blend_name="feature_comparison.blend")

    print(f"run_retopo_job: decimation feature comparison done out_dir={out_dir}")
    return 0


def _save_and_export(bpy, out_dir, result_objs, *, blend_name="lowpoly.blend", fbx_name="lowpoly.fbx") -> None:
    """Best-effort save of the scene and FBX export of ``result_objs`` (plan §8)."""
    try:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(os.path.join(out_dir, blend_name)))
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        print(f"run_retopo_job: save .blend skipped ({exc})")
    try:
        for o in bpy.context.view_layer.objects:
            o.select_set(o in result_objs)
        if result_objs:
            bpy.context.view_layer.objects.active = result_objs[0]
        bpy.ops.export_scene.fbx(
            filepath=os.path.abspath(os.path.join(out_dir, fbx_name)),
            use_selection=True,
        )
    except Exception as exc:  # noqa: BLE001 - export is best-effort
        print(f"run_retopo_job: FBX export skipped ({exc})")


def _run_batch(bpy, job, plan, obj, out_dir, levels, targets,
               preserve_features=False, feature_angle=30.0, voxel_adaptivity=0.0) -> int:
    """Phase 4: generate + compare several LODs from one high-poly object."""
    from retopo_agent.blender.batch import generate_and_evaluate_lods
    from retopo_agent.levels import plan_topology_levels

    plans = plan_topology_levels(len(obj.data.polygons), levels=levels or None, targets=targets or None)
    print(f"run_retopo_job: batch LODs {[ (p.level, p.target_face_count) for p in plans ]}")

    comparison, results = generate_and_evaluate_lods(
        obj,
        plans,
        quad_required=plan["quad_required"],
        ngon_allowed=plan["ngon_allowed"],
        apply_shrinkwrap=_as_bool(job.get("apply_shrinkwrap"), True),
        preserve_sharp=plan["feature_policy"]["preserve_hard_edges"],
        expect_closed=_as_bool(job.get("expect_closed"), True),
        preserve_features=preserve_features,
        feature_angle=feature_angle,
        voxel_adaptivity=voxel_adaptivity,
    )

    with open(os.path.join(out_dir, "comparison.json"), "w", encoding="utf-8") as fh:
        json.dump(comparison.to_dict(), fh, indent=2)
    for entry in comparison.entries:
        print(
            f"run_retopo_job: LOD {entry.level} target={entry.target_face_count} "
            f"actual={entry.actual_face_count} (err={entry.target_error_ratio:.3f}) "
            f"validation={entry.validation_status} shape={entry.shape_status} "
            f"quad_ratio={entry.quad_ratio:.3f}"
        )

    if not _as_bool(job.get("keep_source"), False):
        _remove_object(bpy, obj)

    lod_objs = [r.obj for r in results]
    try:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(os.path.join(out_dir, "lowpoly_lods.blend")))
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        print(f"run_retopo_job: save .blend skipped ({exc})")
    try:
        for o in bpy.context.view_layer.objects:
            o.select_set(o in lod_objs)
        if lod_objs:
            bpy.context.view_layer.objects.active = lod_objs[0]
        bpy.ops.export_scene.fbx(
            filepath=os.path.abspath(os.path.join(out_dir, "lowpoly_lods.fbx")),
            use_selection=True,
        )
    except Exception as exc:  # noqa: BLE001 - export is best-effort
        print(f"run_retopo_job: FBX export skipped ({exc})")

    print(f"run_retopo_job: batch done ({len(comparison.entries)} LODs) out_dir={out_dir}")
    return 0


def _remove_object(bpy, obj) -> None:
    """Delete an object and its now-unused mesh data from the scene."""
    mesh = obj.data if obj.type == "MESH" else None
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    except (RuntimeError, ReferenceError) as exc:
        print(f"run_retopo_job: could not remove source object ({exc})")


if __name__ == "__main__":
    sys.exit(main())
