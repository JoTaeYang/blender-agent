"""App-facing UV generate + optimize worker (Electron MVP 3 plan §4, §10; Sessions A–D).

Run inside Blender:

    blender --background --python worker/generate_uv_from_seams.py -- --job /abs/job.json

``job.json`` carries ``command: "generate_uv_from_seams"`` (plan §4.1):

1. open/import the working model + resolve the selected object,
2. load the MVP 2 ``user_seam_spec.json`` and VALIDATE its edge ids against the
   current mesh (invalid ids / object mismatch -> structured ``failed``, plan §4.1),
3. run ``chart_uv_agent.pipeline.run_chart_uv`` in STRICT user/reference mode —
   the user's seam set is the source of truth and is never changed (plan §1, §6),
4. evaluate layout-optimization candidates over that FIXED seam set and pick the
   best safe candidate (or keep the baseline) (plan §5),
5. render baseline vs selected UV-layout + checker previews (plan §7),
6. save ``selected_uv.blend`` and, on an accepted run, copy it + the summary into
   ``work/uv/`` for the MVP 4/5 handoff (plan §9),
7. normalize ``p5_gate.json`` / ``seam_report.json`` / ``candidate_summary.json``
   and write ``uv_generate_summary.json`` + the ``status.json`` lifecycle.

Hard rules (plan §1, §6, §14): the worker NEVER overwrites the source working
model, NEVER overwrites the user seam spec, and NEVER auto-changes the seam set.
A run that breaks seam integrity (``auto_added_seams != 0`` or
``final_seam_count != user_seam_count``) or still has blocking overlap ends
``needs_user_review`` and does NOT replace ``work/uv/selected_uv.blend`` (plan §6).
Every exit leaves a structured JSON result so the app never parses stdout (plan §4.1).
"""

from __future__ import annotations

import os
import shutil
import sys
import traceback


def _parse_args(argv: list[str]) -> dict:
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
                opts[key] = argv[i + 1]
                i += 2
            else:
                opts[key] = "true"
                i += 1
        else:
            i += 1
    return opts


def _ensure_importable() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for p in (root, here):
        if p not in sys.path:
            sys.path.insert(0, p)


def _open_model(bpy, path: str) -> None:
    """Open a ``.blend`` or import a model into a fresh scene (plan §4.1 import set)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
        return
    try:
        bpy.ops.wm.read_homefile(use_empty=True)
    except Exception:  # noqa: BLE001 - best-effort; default scene is acceptable
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:  # pragma: no cover - legacy Blender
            bpy.ops.import_scene.obj(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    else:
        raise ValueError(f"unsupported model format: {ext or '(none)'}")


def _mesh_objects(bpy) -> list:
    return [o for o in bpy.data.objects if o.type == "MESH"]


def _resolve_object(bpy, object_name):
    obj = bpy.data.objects.get(object_name) if object_name else None
    if obj is None or obj.type != "MESH":
        obj = next((o for o in _mesh_objects(bpy)), None)
    return obj


def _model_label(job: dict) -> str | None:
    rel = job.get("model_rel")
    if rel:
        return rel
    model = job.get("model")
    return os.path.basename(model) if model else None


def _seam_spec_label(job: dict) -> str | None:
    rel = job.get("seam_spec_rel")
    if rel:
        return rel
    sp = job.get("seam_spec")
    return os.path.basename(sp) if sp else None


def _status_input(job: dict) -> dict:
    return {
        "model": _model_label(job),
        "object_name": job.get("object_name"),
        "seam_spec": _seam_spec_label(job),
    }


# ---------------------------------------------------------------------------
# Spec validation (plan §6 — pre-run edge-id / object checks)
# ---------------------------------------------------------------------------
def _validate_spec_against_mesh(spec, mesh, object_name: str | None) -> dict:
    """Edge-id range + object-match validation (plan §4.1, §6).

    Returns ``{"invalid_edges": [...], "object_mismatch": bool}``. Invalid edge
    ids or an object mismatch make the run a structured ``failed`` BEFORE the
    engine runs, so nothing ships (plan §4.1, §6).
    """
    n = mesh.edge_count
    edges = set(spec.effective_seam_edges()) | set(spec.effective_protected_edges())
    invalid = sorted(e for e in edges if not (0 <= e < n))
    mismatch = bool(spec.object and object_name and spec.object != object_name)
    return {"invalid_edges": invalid, "object_mismatch": mismatch}


# ---------------------------------------------------------------------------
# Seam source resolution (UV-boundary-fallback revision plan §1, §4.1)
# ---------------------------------------------------------------------------
def _resolve_seam_source(contract, job: dict, obj) -> dict:
    """Resolve the seam source for a Generate run (revision plan §1, §4.1).

    Precedence (``decide_seam_source``): an existing ``seam_spec`` file is the
    source of truth; else the selected/active ``uv_layer`` is read and its UV
    island boundary becomes a *derived* ``UserSeamSpec``; else ``needs_input``.
    The derived path NEVER overwrites the MVP 2 ``user_seam_spec.json`` and NEVER
    adds a seam beyond the UV boundary (revision plan §4.1 "Do not").

    Returns a dict:

    - ``{"status": "ok", "spec": UserSeamSpec, "seam_source": <block>,
        "label": <str>, "derived_spec": <dict|None>, "resolution": <report>}``
    - ``{"status": "needs_input", "error": {...}, "resolution": <report>}``
    - ``{"status": "failed", "error": {...}, "resolution": <report>}`` for a
      malformed spec / unreadable UV (a structured pre-run failure, plan §4.1).
    """
    from artist_uv_agent.user_seams import UserSeamSpec, load_user_seam_spec
    from uv_agent.blender.uv_extract import extract_uv_boundary_edges

    seam_spec_path = job.get("seam_spec")
    seam_spec_exists = bool(seam_spec_path and os.path.exists(seam_spec_path))
    uv_layer = job.get("uv_layer") or job.get("selected_uv_layer")
    policy = job.get("seam_source_policy", contract.DEFAULT_SEAM_SOURCE_POLICY)
    decision = contract.decide_seam_source(
        seam_spec_path=seam_spec_path, seam_spec_exists=seam_spec_exists,
        uv_layer=uv_layer, policy=policy)

    # 1) Explicit MVP 2 spec wins (revision plan §1 case 1, §7).
    if decision["kind"] == contract.SEAM_SOURCE_USER_SPEC:
        try:
            spec = load_user_seam_spec(seam_spec_path)
        except Exception as exc:  # noqa: BLE001 - malformed spec is a setup error
            return {"status": "failed", "resolution": {"policy": policy, "kind": "failed"},
                    "error": {"code": "invalid_seam_spec", "message": f"could not load seam spec: {exc}"}}
        path = job.get("seam_spec_rel") or os.path.basename(seam_spec_path)
        seam_source = contract.build_seam_source(
            source_type=contract.SEAM_SOURCE_USER_SPEC, path=path, uv_layer=None,
            user_confirmed=True, derived=False)
        return {"status": "ok", "spec": spec, "seam_source": seam_source, "label": path,
                "derived_spec": None,
                "resolution": {"policy": policy, "kind": decision["kind"], "seam_spec": path}}

    # 2) Derive a spec from the existing UV island boundary (revision plan §1 case 2).
    if decision["kind"] == contract.SEAM_SOURCE_UV_BOUNDARY:
        try:
            edge_ids, report = extract_uv_boundary_edges(obj, uv_layer)
        except Exception as exc:  # noqa: BLE001 - unreadable UV is a structured failure
            return {"status": "failed",
                    "resolution": {"policy": policy, "kind": "failed", "uv_layer": uv_layer},
                    "error": {"code": "uv_boundary_extract_failed",
                              "message": f"UV boundary extraction failed: {exc}"}}
        # Requested fallback layer is missing/empty -> needs_input (revision plan §1 case 3).
        if report.get("uv_layer_missing"):
            return {"status": contract.STATUS_NEEDS_INPUT,
                    "resolution": {"policy": policy, "kind": contract.STATUS_NEEDS_INPUT,
                                   "requested_uv_layer": uv_layer, "uv_layer_missing": True},
                    "error": {"code": contract.MISSING_SEAM_SOURCE_CODE,
                              "message": contract.MISSING_SEAM_SOURCE_MESSAGE}}
        resolved_layer = report.get("uv_layer") or uv_layer
        derived_spec = contract.make_derived_seam_spec(
            object_name=obj.name, user_seam_edges=edge_ids, uv_layer=resolved_layer)
        spec = UserSeamSpec.from_dict(derived_spec)
        path = job.get("derived_seam_spec_out_rel") or contract.DERIVED_SEAM_SPEC_REL
        seam_source = contract.build_seam_source(
            source_type=contract.SEAM_SOURCE_UV_BOUNDARY, path=path, uv_layer=resolved_layer,
            user_confirmed=False, derived=True)
        # Flatten the boundary report's headline fields onto the resolution block so
        # ``seam_source_resolution.json`` is self-explanatory (MVP3 §2 Goal A completion
        # criterion / §3 Step 2): island_count, boundary_edge_count, the extraction method,
        # and any dropped/ambiguous edges that explain a low boundary count.
        return {"status": "ok", "spec": spec, "seam_source": seam_source, "label": path,
                "derived_spec": derived_spec,
                "resolution": {"policy": policy, "kind": decision["kind"],
                               "uv_layer": resolved_layer, "derived_seam_spec": path,
                               "object_name": obj.name,
                               "island_count": report.get("island_count"),
                               "uv_layer_loop_count": report.get("uv_layer_loop_count"),
                               "boundary_edge_count": report.get("boundary_edge_count"),
                               "boundary_extraction_method": report.get("method"),
                               "mesh_boundary_edge_count": report.get("mesh_boundary_edge_count"),
                               "ambiguous_boundary_count": report.get("ambiguous_boundary_count"),
                               "dropped_or_ambiguous_edges": report.get("dropped_or_ambiguous_edges", []),
                               "boundary_report": report}}

    # 3) Nothing to unwrap from (revision plan §1 case 3, §4.2).
    return {"status": contract.STATUS_NEEDS_INPUT, "error": decision["error"],
            "resolution": {"policy": policy, "kind": contract.STATUS_NEEDS_INPUT,
                           "seam_spec": None, "uv_layer": None}}


# ---------------------------------------------------------------------------
# Preview rendering (plan §7 — baseline vs selected, stable framing)
# ---------------------------------------------------------------------------
def _render_previews(obj, mesh, final_seams, out_dir: str, *, render_size: int,
                     texture_size: int, checker_scale: float) -> list[str]:
    """Render the SELECTED then the BASELINE UV-layout + checker previews (plan §7).

    The object enters holding the SELECTED layout (``run_chart_uv`` left it there).
    Selected previews are rendered first; then the baseline strict-seam unwrap is
    re-applied to the SAME fixed seam set and the baseline previews are rendered.
    Camera framing is on the (UV-independent) mesh bounds, so it is identical
    between baseline and selected (plan §7). Best-effort: a failing render becomes
    a warning, not a failure (plan §13). ``selected_uv.blend`` must already be
    saved before this runs (the checker material must not persist, plan §7)."""
    from chart_uv_agent.layout_optimization import BASELINE_SPEC
    from chart_uv_agent.unwrap import read_uvmap, unwrap_and_pack
    from uv_agent.blender.review_render import render_checker_views
    from uv_agent.geometry.uv_review import write_uv_layout_png

    warnings: list[str] = []
    seams = set(int(e) for e in final_seams)

    def _layout(tag: str) -> None:
        try:
            uvmap = read_uvmap(obj, mesh)
            write_uv_layout_png(mesh, uvmap, os.path.join(out_dir, f"{tag}_uv_layout.png"),
                                size=texture_size)
        except Exception as exc:  # noqa: BLE001 - layout is best-effort (plan §7)
            warnings.append(f"{tag}_uv_layout.png render failed: {exc}")

    def _checker(tag: str) -> None:
        checker = render_checker_views(
            obj, out_dir, scale=checker_scale, size=render_size,
            filenames={"front": f"{tag}_checker_front.png", "side": f"{tag}_checker_side.png"})
        for view in ("front", "side"):
            if view not in checker:
                warnings.append(f"{tag}_checker_{view}.png render failed")

    # 1) Selected layout (object already holds it).
    _layout("selected")
    _checker("selected")

    # 2) Baseline = the first strict user-seam unwrap, re-applied on the SAME seams
    #    (plan §7 "Baseline means first strict user seam unwrap before layout
    #    optimization replacement"). Never adds/removes a seam.
    try:
        unwrap_and_pack(obj, seams, margin=BASELINE_SPEC["margin"],
                        method=BASELINE_SPEC["unwrap_method"],
                        minimize_iters=BASELINE_SPEC["minimize_iters"],
                        pack_shape=BASELINE_SPEC["pack_shape"], rotate=BASELINE_SPEC["rotate"],
                        average_scale=BASELINE_SPEC["average_scale"])
    except Exception as exc:  # noqa: BLE001 - baseline preview is best-effort
        warnings.append(f"baseline unwrap failed: {exc}")
        return warnings
    _layout("baseline")
    _checker("baseline")
    return warnings


# ---------------------------------------------------------------------------
# generate_uv_from_seams (plan §4.1, §10)
# ---------------------------------------------------------------------------
def _run_generate(bpy, contract, job: dict, out_dir: str, status_path: str, status: dict) -> int:
    from chart_uv_agent.layout_optimization import BASELINE_SPEC, make_config, spec_id
    from chart_uv_agent.pipeline import run_chart_uv
    from uv_agent.blender.extract import extract_mesh_graph

    run_id = job.get("run_id", "uv_run")
    options = contract.merge_options(job.get("options"))
    model_label = _model_label(job)
    seam_spec_label = _seam_spec_label(job)

    def _write_resolution(resolution: dict | None) -> None:
        if resolution is not None:
            contract.write_json(os.path.join(out_dir, contract.SEAM_SOURCE_RESOLUTION_FILE), resolution)

    def _fail(code: str, message: str, **details) -> int:
        err = {"code": code, "message": message}
        if details:
            err["details"] = details
        contract.finalize_status(status, status=contract.STATUS_FAILED, error=err)
        contract.write_json(status_path, status)
        print(f"generate_uv_from_seams: {message}", file=sys.stderr)
        return 2

    def _needs_input(code: str, message: str) -> int:
        contract.finalize_status(status, status=contract.STATUS_NEEDS_INPUT,
                                 error={"code": code, "message": message})
        contract.write_json(status_path, status)
        print(f"generate_uv_from_seams: needs_input: {message}", file=sys.stderr)
        # needs_input is a product outcome (no usable seam source), not a process
        # error — the verdict lives in status.json, so exit 0 (plan §4.1, revision §4.2).
        return 0

    # --- object -----------------------------------------------------------
    obj = _resolve_object(bpy, job.get("object_name"))
    if obj is None:
        return _fail("object_not_found",
                     f"no mesh object to generate (requested {job.get('object_name')!r})")

    # --- seam source: explicit spec | derived UV boundary | needs_input ---
    # (UV-boundary-fallback revision plan §1, §4.1). The derived path reads an
    # existing UV island boundary; it never auto-adds seams or overwrites the
    # MVP 2 user_seam_spec.json (revision plan §2.2, §4.1).
    resolved = _resolve_seam_source(contract, job, obj)
    _write_resolution(resolved.get("resolution"))
    if resolved["status"] == contract.STATUS_NEEDS_INPUT:
        return _needs_input(resolved["error"]["code"], resolved["error"]["message"])
    if resolved["status"] == "failed":
        return _fail(resolved["error"]["code"], resolved["error"]["message"])

    spec = resolved["spec"]
    seam_source = resolved["seam_source"]
    seam_spec_label = resolved["label"]
    derived_spec = resolved["derived_spec"]

    # Persist a derived spec SEPARATELY (canonical work/seams + run-folder copy);
    # never overwrite the user's MVP 2 spec (revision plan §4.1 "Do not").
    if derived_spec is not None:
        if job.get("derived_seam_spec_out"):
            try:
                contract.write_json(job["derived_seam_spec_out"], derived_spec)
            except Exception as exc:  # noqa: BLE001 - canonical copy is best-effort
                print(f"generate_uv_from_seams: derived spec write failed: {exc}", file=sys.stderr)
        contract.write_json(os.path.join(out_dir, contract.DERIVED_SEAM_SPEC_FILE), derived_spec)

    mesh = extract_mesh_graph(obj)
    validation = _validate_spec_against_mesh(spec, mesh, obj.name)
    if validation["invalid_edges"] or validation["object_mismatch"]:
        msg = ("Seam spec contains edge ids that do not exist on the selected mesh."
               if validation["invalid_edges"]
               else f"Seam spec object {spec.object!r} does not match selected object {obj.name!r}.")
        return _fail("invalid_seam_spec", msg,
                     invalid_edges=validation["invalid_edges"],
                     object_mismatch=validation["object_mismatch"])

    # --- strict user/reference run (plan §1, §6) --------------------------
    optimize_layout = bool(options.get("optimize_layout", True))
    lo_cfg = None
    if optimize_layout:
        lo_cfg = make_config(options.get("layout_opt_preset", contract.DEFAULT_LAYOUT_OPT_PRESET),
                             max_candidates=int(options.get("layout_opt_max_candidates",
                                                            contract.DEFAULT_LAYOUT_OPT_MAX_CANDIDATES)),
                             enabled=True)
    print(f"generate_uv_from_seams: object={obj.name!r} user_seams={len(spec.user_seam_edges)} "
          f"protected={len(spec.user_protected_edges)} optimize_layout={optimize_layout} "
          f"max_candidates={getattr(lo_cfg, 'max_candidates', 0)} "
          f"flags(auto_refine={options['auto_refine_user_seams']},repair={options['repair_user_seams']},"
          f"enforce={options['enforce_user_mandatory']},gate={options['gate_user_mandatory']})", flush=True)

    res = run_chart_uv(
        obj, mesh, user_seam_spec=spec,
        auto_refine_user_seams=bool(options["auto_refine_user_seams"]),
        repair_user_seams=bool(options["repair_user_seams"]),
        enforce_user_mandatory=bool(options["enforce_user_mandatory"]),
        gate_user_mandatory=bool(options["gate_user_mandatory"]),
        optimize_layout=optimize_layout,
        layout_optimization_config=lo_cfg)

    final_seams = res.get("seams", [])
    metrics = res.get("metrics", {})
    user_block = res.get("user_seams", {})
    layout_report = res.get("layout_optimization")
    gate = res.get("gate")

    # --- normalized run reports (plan §3, §4.1) ---------------------------
    contract.write_json(os.path.join(out_dir, contract.P5_GATE_FILE), {
        "engine": "chart", "mode": res.get("mode"), "chart_count": res.get("chart_count"),
        "metrics": metrics, "gate": gate.to_dict() if gate is not None else None,
        "gate_config": res.get("gate_config"), "user_seams": user_block,
        "distortion": res.get("distortion"), "conclusion": res.get("conclusion"),
        "mandatory_90_edges": res.get("mandatory_90_edges"),
        "mandatory_90_missing": res.get("mandatory_90_missing"),
        "mandatory_90_fold_edges": res.get("mandatory_90_fold_edges"),
        "mandatory_90_uv_unsplit": res.get("mandatory_90_uv_unsplit"),
        "initial_island_count": res.get("initial_island_count"),
        "final_island_count": res.get("final_island_count"),
        "seam_type_counts": res.get("seam_type_counts"),
        "layout_optimization": layout_report, "history": res.get("history"),
        "seam_count": len(final_seams), "seams": final_seams,
    })
    seam_report = res.get("seam_report")
    if seam_report is not None:
        contract.write_json(os.path.join(out_dir, contract.SEAM_REPORT_FILE), seam_report)

    candidate_summary = contract.normalize_candidate_summary(
        layout_report, baseline_candidate_id=spec_id(BASELINE_SPEC),
        score_weights=getattr(lo_cfg, "score_weights", None),
        max_candidates=int(options.get("layout_opt_max_candidates",
                                       contract.DEFAULT_LAYOUT_OPT_MAX_CANDIDATES)),
        average_scale=bool(getattr(lo_cfg, "average_scale", True)))
    contract.write_json(os.path.join(out_dir, contract.CANDIDATE_SUMMARY_FILE), candidate_summary)
    selected_candidate_id = candidate_summary.get("selected_candidate_id")

    # --- selected UV blend (saved CLEAN, before any checker material) -----
    warnings: list[str] = []
    save_blend = bool(options.get("save_selected_blend", True))
    run_blend = os.path.join(out_dir, contract.SELECTED_BLEND_FILE)
    blend_saved = False
    if save_blend:
        try:
            bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(run_blend), copy=True)
            blend_saved = True
        except Exception as exc:  # noqa: BLE001 - blend save is best-effort warning
            warnings.append(f"selected_uv.blend save failed: {exc}")

    # --- previews (plan §7) -----------------------------------------------
    if bool(options.get("render_previews", True)):
        warnings += _render_previews(
            obj, mesh, final_seams, out_dir,
            render_size=int(options.get("render_size_px", 900)),
            texture_size=int(options.get("texture_size_px", 1024)),
            checker_scale=float(options.get("checker_scale", 40.0)))

    # --- seam integrity + layout quality -> status (plan §6, §13) ---------
    integrity = contract.evaluate_seam_integrity(
        user_block, options, final_seams=final_seams,
        invalid_edges=user_block.get("invalid_edges"), object_mismatch=False)
    quality = contract.evaluate_layout_quality(metrics)
    run_status = contract.classify_generate_status(integrity, quality)
    for v in integrity["violations"]:
        warnings.append(f"seam integrity: {v.get('code')}")
    for issue in quality["issues"]:
        warnings.append(f"layout quality: {issue.get('code')}")

    # --- handoff (only an accepted run ships to work/uv, plan §6, §9) -----
    selected_uv_model = None
    if run_status == contract.STATUS_ACCEPTED and blend_saved and job.get("selected_blend_out"):
        try:
            os.makedirs(os.path.dirname(job["selected_blend_out"]), exist_ok=True)
            shutil.copyfile(run_blend, job["selected_blend_out"])
            selected_uv_model = job.get("selected_blend_out_rel") or contract.SELECTED_UV_BLEND_REL
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"selected_uv.blend handoff copy failed: {exc}")

    artifacts, art_warnings = contract.collect_generate_artifacts(out_dir)
    warnings = art_warnings + warnings

    summary = contract.build_generate_summary(
        run_id=run_id, status=run_status, model=model_label, object_name=obj.name,
        seam_spec=seam_spec_label, seam_source=seam_source, metrics=metrics,
        seam_integrity=integrity["block"],
        layout_optimization=contract.build_layout_optimization_block(layout_report),
        artifacts=artifacts, selected_candidate_id=selected_candidate_id,
        selected_uv_model=selected_uv_model, warnings=warnings)
    contract.write_json(os.path.join(out_dir, contract.SUMMARY_FILE), summary)

    # Stable handoff copy for MVP 4/5 (one file to read, plan §9).
    if selected_uv_model is not None and job.get("selected_summary_out"):
        try:
            contract.write_json(job["selected_summary_out"], {**summary, "source_run_id": run_id})
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"selected_uv_summary.json copy failed: {exc}")

    contract.finalize_status(status, status=run_status, artifacts=artifacts)
    contract.write_json(status_path, status)
    print(f"generate_uv_from_seams: {run_status} run={run_id} object={obj.name!r} "
          f"selected={selected_candidate_id} kept_baseline={candidate_summary.get('kept_baseline')} "
          f"seam_integrity_valid={integrity['valid']} final_seams={len(final_seams)} "
          f"user_seams={integrity['block']['user_seam_count']} "
          f"auto_added={integrity['block']['auto_added_seams']}", flush=True)
    # needs_user_review is a product outcome, not a process error — the real
    # verdict lives in status.json, so the process still exits 0 (plan §4.1, §6).
    return 0


def main() -> int:
    _ensure_importable()
    import app_uv_generate_contract as contract  # type: ignore

    opts = _parse_args(sys.argv)
    if "job" not in opts:
        print("generate_uv_from_seams requires --job /abs/job.json", file=sys.stderr)
        return 2
    job = contract.read_json(opts["job"])
    command = job.get("command", contract.CMD_GENERATE_UV_FROM_SEAMS)
    if command != contract.CMD_GENERATE_UV_FROM_SEAMS:
        print(f"generate_uv_from_seams: unsupported command {command!r}", file=sys.stderr)
        if job.get("out"):
            contract.write_json(job["out"], contract.error_envelope(
                command or "unknown", f"unsupported command {command!r}", code="bad_command"))
        return 2

    out_dir = job.get("out_dir") or os.path.join("out", job.get("run_id", "uv_run"))
    os.makedirs(out_dir, exist_ok=True)
    status_path = os.path.join(out_dir, "status.json")
    status = contract.new_status(
        run_id=job.get("run_id", "uv_run"), command=command,
        status=contract.STATUS_RUNNING, input=_status_input(job))
    contract.write_json(status_path, status)

    model = job.get("model")
    if not model or not os.path.exists(model):
        contract.finalize_status(status, status=contract.STATUS_FAILED,
                                 error={"code": "model_missing", "message": f"model not found: {model}"})
        contract.write_json(status_path, status)
        print(f"generate_uv_from_seams: model not found: {model}", file=sys.stderr)
        return 2

    ext = os.path.splitext(model)[1].lower()
    if ext not in contract.SUPPORTED_MODEL_EXTS:
        contract.finalize_status(status, status=contract.STATUS_FAILED, error={
            "code": "unsupported_format",
            "message": f"unsupported format {ext!r}; supported: {', '.join(contract.SUPPORTED_MODEL_EXTS)}"})
        contract.write_json(status_path, status)
        print(f"generate_uv_from_seams: unsupported format {ext!r}", file=sys.stderr)
        return 2

    import bpy  # only available inside Blender

    try:
        _open_model(bpy, model)
    except Exception as exc:  # noqa: BLE001 - structured import failure
        contract.finalize_status(status, status=contract.STATUS_FAILED,
                                 error={"code": "import_failed", "message": f"open/import failed: {exc}"})
        contract.write_json(status_path, status)
        print(f"generate_uv_from_seams: open/import failed: {exc}", file=sys.stderr)
        return 3

    try:
        return _run_generate(bpy, contract, job, out_dir, status_path, status)
    except Exception as exc:  # noqa: BLE001 - any failure becomes a structured status
        tb = traceback.format_exc()
        contract.finalize_status(status, status=contract.STATUS_FAILED,
                                 error={"code": "exception", "message": str(exc), "traceback": tb})
        contract.write_json(status_path, status)
        print(f"generate_uv_from_seams: failed: {exc}\n{tb}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
