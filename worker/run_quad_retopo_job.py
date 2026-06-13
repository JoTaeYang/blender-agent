"""Headless quad-retopo + auto-UV worker (quad-retopo plan §12.4).

The end-to-end orchestrator for the QUAD pipeline (distinct from the demoted
decimation worker ``worker/run_retopo_job.py``). It runs the phases P1→P6 and is
resumable per phase: each phase persists its artifact, and ``--from-phase P2``
reopens ``proxy.blend`` instead of re-importing the 1.86 GB OBJ.

    /Applications/Blender.app/Contents/MacOS/Blender --background --python \
        worker/run_quad_retopo_job.py -- \
        --input sample/humanstatue.obj \
        --reference sample/humanstatue_low.obj \
        --target-faces 2900 \
        --proxy-faces 1000000 \
        --out out/humanstatue_job1

Implemented so far: **P1 — scalable ingest + manifold proxy** (plan §7), via
:mod:`retopo_agent.blender.proxy`. P2–P6 are wired as explicit phase stubs that
raise ``NotImplementedError`` until built, so the dispatch/resume scaffold is real
and the next phase only has to fill in its function.

P1 outputs under ``out/<job>/``:

    proxy.blend     the manifold proxy ONLY (original discarded, orphans purged)
    p1_report.json  source summary, proxy build, manifold check, fidelity, timings, RSS
    p1_report.md    human-readable summary of the same
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time

PHASES = ["P1", "P2", "P3", "P4", "P5", "P6"]


def _parse_args(argv: list[str]) -> dict:
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
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
    repo_root = os.path.dirname(here)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def _peak_rss_gb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin" or rss > (1 << 40):
        return round(rss / (1024 ** 3), 2)
    return round(rss / (1024 ** 2), 2)


def main() -> int:
    _ensure_importable()
    import bpy

    opts = _parse_args(sys.argv)
    mode = opts.get("mode", "adaptive").lower()  # §2.6: adaptive (default) | quad
    if mode not in {"adaptive", "quad"}:
        print(f"run_quad_retopo_job: unknown --mode {mode} (adaptive|quad)", file=sys.stderr)
        return 2

    # One process = one target (plan §10 / acceptance discipline). A single Blender
    # session reduces exactly one budget: the heavy proxy is mutated/decimated in
    # place, so reusing it across budgets in one run is a memory + correctness
    # landmine. A future batch-LOD feature must fork a process per budget, NOT loop
    # here. We therefore reject a comma-list of targets outright.
    raw_target = opts.get("target_faces", "5850")  # §1 default T_goal (budget parity)
    if "," in str(raw_target):
        print("run_quad_retopo_job: --target-faces takes ONE budget; one process = one "
              "target. Launch a separate process per budget.", file=sys.stderr)
        return 2
    target_faces = int(raw_target)

    if mode == "quad":
        return _run_quad_mode(bpy, opts, target_faces)
    return _run_adaptive_mode(bpy, opts, target_faces)


def _run_quad_mode(bpy, opts: dict, target_faces: int) -> int:
    """The frozen QuadriFlow path (plan §2: kept compiling + tested, not extended)."""
    inp = opts.get("input", "sample/humanstatue.obj")
    out_dir = opts.get("out", os.path.join("out", "quad_retopo_job"))
    proxy_faces = int(opts.get("proxy_faces", 1_000_000))
    two_stage = _as_bool(opts.get("two_stage"), False)
    preserve_sharp = _as_bool(opts.get("preserve_sharp"), False)
    preserve_boundary = _as_bool(opts.get("preserve_boundary"), False)
    from_phase = opts.get("from_phase", "P1").upper()
    os.makedirs(out_dir, exist_ok=True)

    if from_phase not in PHASES:
        print(f"run_quad_retopo_job: unknown --from-phase {from_phase}", file=sys.stderr)
        return 2
    start = PHASES.index(from_phase)

    proxy_obj = None
    if start <= PHASES.index("P1"):
        rc, proxy_obj = run_p1(bpy, inp, out_dir, proxy_faces)
        if rc != 0:
            return rc
    elif start <= PHASES.index("P2"):
        proxy_obj = _open_proxy(bpy, out_dir)

    if start <= PHASES.index("P2"):
        rc = run_p2(bpy, proxy_obj, out_dir, target_faces,
                    two_stage=two_stage, preserve_sharp=preserve_sharp,
                    preserve_boundary=preserve_boundary)
        if rc != 0:
            return rc

    if start > PHASES.index("P2"):
        raise NotImplementedError(
            f"--from-phase {from_phase} not implemented yet; phases P1 (proxy) and P2 "
            f"(QuadriFlow) exist. P3 will re-project the quad mesh onto the proxy."
        )
    return 0


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _open_proxy(bpy, out_dir: str):
    """Resume path (plan §12.4): open ``proxy.blend`` and return the proxy object."""
    blend = os.path.join(out_dir, "proxy.blend")
    if not os.path.exists(blend):
        raise FileNotFoundError(f"cannot resume: {blend} not found (run P1 first)")
    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(blend))
    obj = bpy.data.objects.get("AI_Proxy")
    if obj is None:
        obj = next((o for o in bpy.data.objects if o.type == "MESH"), None)
    if obj is None:
        raise RuntimeError(f"no proxy mesh found in {blend}")
    print(f"[P2] resumed from {blend}: '{obj.name}' ({len(obj.data.polygons)} faces)", flush=True)
    return obj


def run_p1(bpy, inp: str, out_dir: str, proxy_faces: int):
    """Phase P1 — scalable ingest + manifold proxy build (plan §7).

    Returns ``(return_code, proxy_obj)`` so the same Blender session can flow
    straight into P2 without reopening ``proxy.blend``.
    """
    from retopo_agent.blender.proxy import (
        build_proxy,
        import_source,
        manifold_check,
        persist_proxy,
        proxy_fidelity,
        source_diagnosis,
        source_summary,
    )

    t0 = time.monotonic()
    report: dict = {"phase": "P1", "input": inp, "out_dir": out_dir, "timings_s": {}}

    # Fresh, empty scene so the default cube can't pollute the join.
    bpy.ops.wm.read_factory_settings(use_empty=True)

    print(f"[P1] importing {inp} ...", flush=True)
    source, import_s = import_source(inp)
    report["timings_s"]["import"] = round(import_s, 2)
    summary = source_summary(source)
    report["source"] = summary
    print(
        f"[P1] import {import_s:.1f}s: {summary['faces']} faces "
        f"(quad_ratio={summary['quad_ratio']}, area={summary['total_surface_area']}, "
        f"diag={summary['bbox_diagonal']}, degenerate={summary['degenerate_faces']})  "
        f"rss={_peak_rss_gb()}GB",
        flush=True,
    )

    # Source topology diagnosis (plan §7.2): components / non-manifold / tiny shells.
    # Must run while the original is still loaded; explains whether voxel remesh
    # dropped detached fragments (a real driver of the proxy↔original max distance).
    print("[P1] diagnosing source topology (bmesh) ...", flush=True)
    t = time.monotonic()
    diagnosis = source_diagnosis(source)
    report["timings_s"]["source_diagnosis"] = round(time.monotonic() - t, 2)
    report["source"]["diagnosis"] = diagnosis
    if "error" in diagnosis:
        print(f"[P1] source diagnosis: {diagnosis['error']}", flush=True)
    else:
        print(
            f"[P1] source diagnosis: components={diagnosis['components']} "
            f"(tiny={diagnosis['tiny_component_count']}, "
            f"smallest={diagnosis['smallest_component_faces']} faces, "
            f"largest_ratio={diagnosis['largest_component_ratio']}) "
            f"non_manifold={diagnosis['non_manifold_edges']} "
            f"boundary={diagnosis['boundary_edges']}  rss={_peak_rss_gb()}GB",
            flush=True,
        )

    print(f"[P1] building proxy (target {proxy_faces} faces, voxel-direct) ...", flush=True)
    t = time.monotonic()
    proxy = build_proxy(
        source,
        target_faces=proxy_faces,
        total_area=summary["total_surface_area"],
    )
    report["timings_s"]["proxy_build"] = round(time.monotonic() - t, 2)
    report["proxy"] = proxy.to_dict()
    print(
        f"[P1] proxy: {proxy.proxy_face_count} faces (target {proxy_faces}, "
        f"band={proxy.band}, voxel={proxy.voxel_size:.5g}, "
        f"{proxy.search_iterations} probes)  rss={_peak_rss_gb()}GB",
        flush=True,
    )

    # Drop the stray micro-shell (12-vert floater) so the proxy is a single
    # watertight body and A3/A4 can assert components == 1 (plan §6.2).
    from retopo_agent.blender.proxy import drop_tiny_components

    floater = drop_tiny_components(proxy.obj)
    report["floater_drop"] = floater
    if floater["dropped_components"]:
        print(
            f"[P1] dropped {floater['dropped_components']} tiny component(s) / "
            f"{floater['dropped_faces']} faces (< {floater['threshold_faces']} faces): "
            f"components {floater['components_before']} -> {floater['components_after']}",
            flush=True,
        )

    manifold = manifold_check(proxy.obj)
    report["manifold_check"] = manifold
    print(
        f"[P1] manifold: non_manifold={manifold['non_manifold_edges']} "
        f"boundary={manifold['boundary_edges']} components={manifold['components']} "
        f"is_manifold={manifold['is_manifold']}",
        flush=True,
    )

    # Fidelity MUST run while the original still exists (plan §7.5).
    print("[P1] measuring proxy fidelity vs original ...", flush=True)
    t = time.monotonic()
    fidelity, dist_pcts = proxy_fidelity(source, proxy.obj, voxel_size=proxy.voxel_size)
    report["timings_s"]["fidelity"] = round(time.monotonic() - t, 2)
    report["fidelity"] = {**fidelity.to_dict(), "distance_distribution": dist_pcts}
    print(
        f"[P1] fidelity (original->proxy): status={fidelity.status} "
        f"mean_ratio={fidelity.surface_distance_mean_ratio:.5f} "
        f"max_ratio={fidelity.surface_distance_max_ratio:.5f} "
        f"normal_dev={fidelity.normal_deviation_mean_deg:.2f}deg",
        flush=True,
    )
    if dist_pcts:
        print(
            f"[P1] fidelity distance dist: p50={dist_pcts['p50']} p90={dist_pcts['p90']} "
            f"p99={dist_pcts['p99']} max={dist_pcts['max']} "
            f"(mean={dist_pcts['mean']} = {dist_pcts.get('mean_over_voxel')}x voxel, "
            f"p99={dist_pcts.get('p99_over_voxel')}x voxel)",
            flush=True,
        )

    # Persist proxy.blend with ONLY the proxy; the 24.9M original is discarded here.
    blend_path = os.path.join(out_dir, "proxy.blend")
    persist_proxy(proxy.obj, source, blend_path)
    report["proxy_blend"] = blend_path
    print(f"[P1] saved proxy.blend -> {blend_path} (original discarded)", flush=True)

    report["warnings"] = _p1_warnings(proxy, manifold, fidelity)
    report["timings_s"]["total"] = round(time.monotonic() - t0, 2)
    report["peak_rss_gb"] = _peak_rss_gb()

    with open(os.path.join(out_dir, "p1_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(os.path.join(out_dir, "p1_report.md"), "w", encoding="utf-8") as fh:
        fh.write(_p1_markdown(report))

    print(
        f"[P1] done in {report['timings_s']['total']}s, peak {report['peak_rss_gb']}GB -> {out_dir}",
        flush=True,
    )
    for w in report["warnings"]:
        print(f"[P1] WARNING: {w}", flush=True)
    return 0, proxy.obj


def run_p2(bpy, proxy_obj, out_dir: str, target_faces: int, *,
           two_stage: bool, preserve_sharp: bool, preserve_boundary: bool) -> int:
    """Phase P2 — QuadriFlow quad remesh with target control loop (plan §8).

    Drives the proxy to ~``target_faces`` pure quads, hard-asserting pure-quad /
    manifold / component-bound on every attempt, and persists ``quad.blend`` with
    BOTH the quad mesh and the proxy (P3 needs the proxy as the shrinkwrap target).
    """
    from retopo_agent.blender.quadremesh import quad_remesh_proxy

    t0 = time.monotonic()
    report: dict = {"phase": "P2", "target_faces": target_faces, "out_dir": out_dir}

    print(
        f"[P2] QuadriFlow remesh: proxy {len(proxy_obj.data.polygons)} faces -> "
        f"target {target_faces} quads (two_stage={two_stage}, "
        f"preserve_sharp={preserve_sharp}, preserve_boundary={preserve_boundary}) ...",
        flush=True,
    )
    result = quad_remesh_proxy(
        proxy_obj, target_faces,
        two_stage=two_stage, preserve_sharp=preserve_sharp, preserve_boundary=preserve_boundary,
    )
    report["quadriflow"] = result.to_dict()
    report["timings_s"] = {"quad_remesh": round(time.monotonic() - t0, 2)}
    report["peak_rss_gb"] = _peak_rss_gb()

    m = result.metrics
    print(
        f"[P2] result: {m['faces']} faces (target {target_faces}, band={m['band']}, "
        f"quad_ratio={m['quad_ratio']}, tris={m['tris']}, ngons={m['ngons']}, "
        f"non_manifold={m['non_manifold_edges']}, components={m['components']}/{result.component_bound}) "
        f"seed={result.seed} accepted={result.accepted}  rss={report['peak_rss_gb']}GB",
        flush=True,
    )
    cov = result.coverage or {}
    if cov:
        p2q = cov.get("proxy_to_quad", {})
        print(
            f"[P2] coverage (proxy->quad, pre-shrinkwrap, INFORMATIONAL — P3 gate): "
            f"bbox_min={cov['bbox']['min_ratio']} bbox_max={cov['bbox']['max_ratio']} "
            f"max_ratio={p2q.get('max_ratio')} p99_ratio={p2q.get('p99_ratio')} "
            f"meets_gate_now={cov['passes']}",
            flush=True,
        )
    for a in result.attempts:
        print(f"[P2]   attempt seed={a.get('seed')} faces={a.get('faces')} "
              f"quad_ratio={a.get('quad_ratio')} pure_quad={a.get('pure_quad')} "
              f"exploded={a.get('exploded')} passes={a.get('passes_asserts')}", flush=True)

    # Persist quad.blend with the quad mesh AND the proxy (P3 shrinkwrap target).
    blend_path = os.path.join(out_dir, "quad.blend")
    _purge_then_save(bpy, blend_path)
    report["quad_blend"] = blend_path

    with open(os.path.join(out_dir, "p2_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(os.path.join(out_dir, "p2_report.md"), "w", encoding="utf-8") as fh:
        fh.write(_p2_markdown(report, result))

    print(f"[P2] saved quad.blend -> {blend_path}", flush=True)
    if not result.accepted:
        print(
            "[P2] WARNING: best quad mesh did NOT pass the hard asserts/band — "
            "P4 retry ladder needed (mesh kept, never silently accepted)",
            flush=True,
        )
    print(f"[P2] done in {report['timings_s']['quad_remesh']}s -> {out_dir}", flush=True)
    return 0


def _purge_then_save(bpy, filepath: str) -> None:
    try:
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    except RuntimeError:
        pass
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(filepath))


def _p2_markdown(report: dict, result) -> str:
    m = result.metrics
    lines = [
        "# Phase P2 — QuadriFlow quad remesh report",
        "",
        f"- proxy: `{report['out_dir']}/proxy.blend` → quad: `{report.get('quad_blend')}`",
        f"- target: {report['target_faces']:,} quads (§1 budget) | "
        f"time: {report['timings_s']['quad_remesh']}s | peak RSS {report['peak_rss_gb']} GB",
        f"- **accepted: {result.accepted}** (seed {result.seed}, "
        f"requested {result.requested_faces}, two_stage {result.two_stage})",
        "",
        "## Result metrics (hard asserts)",
        f"- faces: {m['faces']:,} (band={m['band']}, err {m['target_error_ratio']})",
        f"- quad ratio: **{m['quad_ratio']}** | tris: {m['tris']} | n-gons: {m['ngons']} | "
        f"**pure_quad: {m['pure_quad']}**",
        f"- non-manifold edges: {m['non_manifold_edges']} | "
        f"components: {m['components']} / bound {result.component_bound}",
        f"- **passes asserts: {m['passes_asserts']}**",
        "",
        "## Attempts",
    ]
    for a in result.attempts:
        lines.append(f"- {a}")
    lines.append("")
    return "\n".join(lines)


def _p1_warnings(proxy, manifold, fidelity) -> list[str]:
    from retopo_agent.blender.proxy import PROXY_FACE_MAX, PROXY_FACE_MIN

    warnings: list[str] = []
    if not manifold["is_manifold"]:
        warnings.append(
            f"proxy is NOT strictly manifold (non_manifold={manifold['non_manifold_edges']}, "
            f"boundary={manifold['boundary_edges']}); QuadriFlow's input contract is violated"
        )
    if not (PROXY_FACE_MIN <= proxy.proxy_face_count <= PROXY_FACE_MAX):
        warnings.append(
            f"proxy face count {proxy.proxy_face_count} is outside the {PROXY_FACE_MIN}-"
            f"{PROXY_FACE_MAX} band; flow-quality judgments may be made on an under/over-"
            f"resolved proxy"
        )
    if fidelity.status == "failed":
        warnings.append(
            f"proxy fidelity FAILED (max_ratio={fidelity.surface_distance_max_ratio:.4f}); "
            f"the proxy lost too much detail vs the original"
        )
    return warnings


def _source_diagnosis_md(diag: dict, seconds) -> str:
    if not diag:
        return "- diagnosis: (not run)"
    if "error" in diag:
        return f"- diagnosis: {diag['error']}"
    return (
        f"- diagnosis ({seconds}s): components {diag['components']} "
        f"(tiny {diag['tiny_component_count']}, smallest {diag['smallest_component_faces']} faces, "
        f"largest ratio {diag['largest_component_ratio']}), "
        f"non-manifold {diag['non_manifold_edges']}, boundary {diag['boundary_edges']}"
    )


def _p1_markdown(report: dict) -> str:
    s = report["source"]
    p = report["proxy"]
    m = report["manifold_check"]
    f = report["fidelity"]
    t = report["timings_s"]
    lines = [
        "# Phase P1 — Ingest + Proxy report",
        "",
        f"- input: `{report['input']}`",
        f"- proxy: `{report['proxy_blend']}`",
        f"- total: **{t.get('total')}s**, peak RSS **{report['peak_rss_gb']} GB**",
        "",
        "## Source (original)",
        f"- faces: {s['faces']:,} | verts: {s['verts']:,} | quad_ratio: {s['quad_ratio']} "
        f"| degenerate: {s.get('degenerate_faces')}",
        f"- surface area: {s['total_surface_area']} | bbox diagonal: {s['bbox_diagonal']}",
        f"- import time: {t.get('import')}s",
        _source_diagnosis_md(s.get("diagnosis", {}), t.get("source_diagnosis")),
        "",
        "## Proxy (voxel-direct)",
        f"- faces: {p['proxy_face_count']:,} (target {p['target_face_count']:,}, "
        f"band={p['band']}, err={p['target_error_ratio']})",
        f"- voxel size: {p['voxel_size']} (initial {p['initial_voxel_size']}, "
        f"{p['search_iterations']} probes)",
        f"- build time: {t.get('proxy_build')}s",
        f"- search history: {p['search_history']}",
        "",
        "## Manifold check (QuadriFlow input contract)",
        f"- non-manifold edges: {m['non_manifold_edges']} | boundary edges: {m['boundary_edges']}",
        f"- components: {m['components']} | **is_manifold: {m['is_manifold']}**",
        "",
        "## Fidelity (original surface → proxy; bounds the downstream error budget)",
        f"- status: **{f['status']}**",
        f"- surface distance mean: {f['surface_distance_mean']} "
        f"(ratio {f['surface_distance_mean_ratio']})",
        f"- surface distance max: {f['surface_distance_max']} "
        f"(ratio {f['surface_distance_max_ratio']})",
        f"- normal deviation mean: {f['normal_deviation_mean_deg']}°",
        f"- distance distribution: {f.get('distance_distribution', {})}",
        f"- fidelity time: {t.get('fidelity')}s",
        "",
    ]
    if report.get("warnings"):
        lines.append("## Warnings")
        lines += [f"- {w}" for w in report["warnings"]]
        lines.append("")
    return "\n".join(lines)


# ======================================================================== #
#  Adaptive mode — the DEFAULT pipeline (plan §4): P1 → A2 → A3 → A4 → P5 → P6
# ======================================================================== #

ADAPTIVE_PHASES = ["P1", "A2", "A3", "A4", "P5", "P6"]


def _run_adaptive_mode(bpy, opts: dict, target_faces: int) -> int:
    """Default adaptive-decimation pipeline (plan §4). One Blender process drives one
    budget end to end: ingest+proxy (P1), adaptive decimation (A2), tris→quads cleanup
    (A3), the silhouette quality gate + retry ladder (A4), auto-UV (P5) and export +
    fixed-camera renders (P6). Returns 0 on a gate-passing ship, 3 on a best-effort
    (gate-failed) ship — artifacts are written either way, never silently."""
    inp = opts.get("input", "sample/humanstatue.obj")
    reference = opts.get("reference", "sample/humanstatue_low.obj")
    out_dir = opts.get("out", os.path.join("out", "adaptive_job"))
    proxy_faces = int(opts.get("proxy_faces", 1_000_000))
    from_phase = opts.get("from_phase", "P1").upper()
    os.makedirs(out_dir, exist_ok=True)
    if from_phase not in ADAPTIVE_PHASES:
        print(f"run_quad_retopo_job: unknown --from-phase {from_phase} for adaptive mode "
              f"({ADAPTIVE_PHASES})", file=sys.stderr)
        return 2

    t0 = time.monotonic()

    # --- P5 resume (UV repair plan §6): reopen the A4-accepted mesh blend and run only
    # P5 (organic UV) + P6 (export/render). Does NOT redo P1–A4.
    if from_phase == "P5":
        return _run_adaptive_p5_resume(bpy, opts, target_faces, reference, out_dir, t0)

    # --- P1: proxy (fresh build, floater dropped inside) or resume proxy.blend.
    if from_phase == "P1":
        rc, proxy = run_p1(bpy, inp, out_dir, proxy_faces)
        if rc != 0:
            return rc
    else:
        proxy = _open_proxy(bpy, out_dir)
        # A pre-existing proxy.blend (built before the floater-drop change) may still
        # carry the micro-shell; drop it defensively (no-op once already clean).
        from retopo_agent.blender.proxy import drop_tiny_components
        fl = drop_tiny_components(proxy)
        if fl["dropped_components"]:
            print(f"[A2] dropped {fl['dropped_components']} stray shell(s) / "
                  f"{fl['dropped_faces']} faces from resumed proxy", flush=True)

    # --- Baseline: the ground-truth reference measured vs the SAME proxy (plan §7).
    ref, baseline = _measure_reference_baseline(bpy, proxy, reference)
    print(f"[A4] reference baseline vs proxy: {baseline.to_dict()}", flush=True)

    # --- A2/A3/A4: generate + gate, climbing the retry ladder until pass/exhausted.
    gen = _run_adaptive_generate(bpy, proxy, target_faces, baseline, out_dir)
    low, gate = gen["low"], gen["gate"]

    # Free the heavy proxy before P5/P6 (the reference stays for the renders). This
    # is what kept the earlier export from OOM-killing the process.
    proxy_faces_now = len(proxy.data.polygons)
    _remove_object_w(bpy, proxy)
    print(f"[A4] freed proxy ({proxy_faces_now} faces) before UV/export", flush=True)

    # --- P5: auto-UV on the accepted (or best-effort) mesh.
    p5 = run_p5_uv(bpy, low, ref, out_dir, engine=opts.get("uv_engine", "auto"))

    # --- P6: export OBJ (v/vt/vn) + blend + fixed-camera side-by-side renders.
    p6 = run_p6_export(bpy, low, ref, out_dir, target_faces, reference)

    report = {
        "mode": "adaptive",
        "input": inp,
        "reference": reference,
        "target_faces": target_faces,
        "verdict": gate.verdict,
        "gate_passed": gate.passed,
        "baseline": baseline.to_dict(),
        "generation": gen["report"],
        "p5_uv": p5,
        "p6_export": p6,
        "timings_s": {"total": round(time.monotonic() - t0, 1)},
        "peak_rss_gb": _peak_rss_gb(),
    }
    with open(os.path.join(out_dir, "adaptive_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    with open(os.path.join(out_dir, "adaptive_report.md"), "w", encoding="utf-8") as fh:
        fh.write(_adaptive_markdown(report))

    status = "PASS" if gate.passed else "FAILED (best-effort shipped, see report)"
    print(f"[done] adaptive {target_faces}: gate {status}; "
          f"{report['timings_s']['total']}s peak {report['peak_rss_gb']}GB -> {out_dir}",
          flush=True)
    return 0 if gate.passed else 3


def _run_adaptive_p5_resume(bpy, opts, target_faces, reference, out_dir, t0) -> int:
    """Resume at P5 (UV repair plan §6): reopen the A4-accepted mesh blend, import the
    reference, and run organic P5 + P6 only — P1–A4 are NOT redone."""
    blend = opts.get("mesh_blend") or os.path.join(out_dir, f"adaptive_t{target_faces}.blend")
    if not os.path.exists(blend):
        print(f"run_quad_retopo_job: P5 resume needs {blend} (run A2–A4 first)", file=sys.stderr)
        return 2
    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(blend))
    low = bpy.data.objects.get(f"AI_Adaptive_{target_faces}") or \
        next((o for o in bpy.data.objects if o.type == "MESH"), None)
    if low is None:
        print(f"run_quad_retopo_job: no mesh in {blend}", file=sys.stderr)
        return 2
    print(f"[P5] resumed '{low.name}' ({len(low.data.polygons)} faces) from {blend}", flush=True)

    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(filepath=os.path.abspath(reference))
    ref = next(o for o in bpy.data.objects if o not in before and o.type == "MESH")
    ref.name = "AI_Reference"

    p5 = run_p5_uv(bpy, low, ref, out_dir, engine=opts.get("uv_engine", "auto"))
    p6 = run_p6_export(bpy, low, ref, out_dir, target_faces, reference)

    report = {
        "mode": "adaptive", "phase": "P5_resume", "reference": reference,
        "target_faces": target_faces, "from_blend": blend,
        "verdict": p5["gate_verdict"], "uv_shippable": p5["shippable"],
        "p5_uv": p5, "p6_export": p6,
        "timings_s": {"total": round(time.monotonic() - t0, 1)}, "peak_rss_gb": _peak_rss_gb(),
    }
    with open(os.path.join(out_dir, "p5_resume_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"[done] P5 resume {target_faces}: UV gate {p5['gate_verdict']} "
          f"(shippable={p5['shippable']}, fallback_used=False); "
          f"{report['timings_s']['total']}s -> {out_dir}", flush=True)
    return 0 if p5["shippable"] else 3


def _measure_reference_baseline(bpy, proxy, reference_path: str):
    """Import the ground-truth reference and measure it vs the proxy in the same world
    space (plan §7 'Baseline first'): proxy→reference max/p99 distance and the
    reference→proxy mean distance + normal deviation. Returns ``(ref_obj, baseline)``."""
    from retopo_agent.blender.quadremesh import directional_coverage
    from retopo_agent.blender.shape import evaluate_shape_match_blender
    from retopo_agent.geometry.adaptive_gate import ReferenceBaseline
    from retopo_agent.geometry.shape_eval import DECIMATION_SHAPE_THRESHOLDS

    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(filepath=os.path.abspath(reference_path))
    ref = next(o for o in bpy.data.objects if o not in before and o.type == "MESH")
    ref.name = "AI_Reference"
    print(f"[A4] reference '{ref.name}': {len(ref.data.polygons)} faces / "
          f"{len(ref.data.vertices)} verts", flush=True)

    p2r = directional_coverage(proxy, ref)
    r2p = evaluate_shape_match_blender(proxy, ref, thresholds=DECIMATION_SHAPE_THRESHOLDS)
    baseline = ReferenceBaseline(
        proxy_to_ref_max=float(p2r["max"]),
        proxy_to_ref_p99=float(p2r["p99"]),
        ref_to_proxy_mean=float(r2p.surface_distance_mean),
        ref_to_proxy_normal_dev=float(r2p.normal_deviation_mean_deg),
        ref_vertex_count=len(ref.data.vertices),
    )
    return ref, baseline


def _adaptive_gate_metrics(low, proxy) -> dict:
    """Flat metric record for one candidate, consumed by ``evaluate_gate`` (plan §7)."""
    from retopo_agent.blender.adaptive_decimate import (
        _low_to_proxy_shape, _mesh_face_breakdown, _mesh_topology,
    )
    from retopo_agent.blender.quadremesh import bbox_axis_coverage, directional_coverage

    bd = _mesh_face_breakdown(low)
    topo = _mesh_topology(low)
    p2l = directional_coverage(proxy, low)
    return {
        "ngons": bd["ngons"], "tris": bd["tris"], "quads": bd["quads"],
        "non_manifold_edges": topo["non_manifold_edges"], "components": topo["components"],
        "faces": bd["faces"], "vertex_count": len(low.data.vertices),
        "bbox_per_axis": bbox_axis_coverage(low, proxy)["per_axis"],
        "proxy_to_low": {k: p2l.get(k) for k in ("max", "p99", "max_ratio", "p99_ratio")},
        "low_to_proxy": _low_to_proxy_shape(low, proxy),
    }


def _run_adaptive_generate(bpy, proxy, target_faces: int, baseline, out_dir: str) -> dict:
    """A2 + A3 + A4 with the §7 retry ladder. Runs the generator, cleans it, gates it,
    and — if a gate fails — climbs the cheap→expensive ladder by adjusting the next
    attempt's params, keeping only the best candidate object in memory. Returns the
    selected ``low`` object, its ``gate``, and a per-attempt ``report``."""
    from retopo_agent.blender.adaptive_decimate import (
        CleanupAssertionError, adaptive_decimate_proxy, cleanup_to_mixed_poly,
    )
    from retopo_agent.geometry.adaptive_gate import (
        RUNG_DENSER_PROXY, RUNG_FEATURE_PROTECT, RUNG_RATIO_REAIM, RUNG_REPORT_FAILED,
        RUNG_SHRINKWRAP, RUNG_TRIQUAD_TWEAK, GateThresholds, evaluate_gate, next_rung,
    )
    from retopo_agent.geometry.target_search import target_error_ratio

    a2_kwargs = {"shrinkwrap": True, "preserve_features": False, "preserve_features_strength": 1.0}
    a3_kwargs = {"face_threshold_deg": 15.0, "shape_threshold_deg": 15.0}
    attempted_rungs: list[str] = []
    attempts: list[dict] = []
    best = None  # {"low", "gate", "score", "rec"}

    def score(gate, metrics) -> tuple:
        return (0 if gate.passed else 1, len(gate.hard_failures),
                target_error_ratio(metrics["faces"], target_faces))

    rung = "initial"
    while True:
        t = time.monotonic()
        a2 = adaptive_decimate_proxy(proxy, target_faces, **a2_kwargs)
        low = a2.obj
        a3 = None
        try:
            a3 = cleanup_to_mixed_poly(low, target_face_count=target_faces, component_bound=1, **a3_kwargs)
            a3_ok = True
        except CleanupAssertionError as exc:
            a3 = {"error": str(exc), "asserts": exc.asserts}
            a3_ok = False

        metrics = _adaptive_gate_metrics(low, proxy)
        gate = evaluate_gate(metrics, target_face_count=target_faces, baseline=baseline,
                             thresholds=GateThresholds())
        rec = {
            "rung": rung, "a2_kwargs": dict(a2_kwargs), "a3_kwargs": dict(a3_kwargs),
            "a2": a2.to_dict(), "a3": a3, "a3_ok": a3_ok,
            "metrics": metrics, "gate": gate.to_dict(),
            "wall_s": round(time.monotonic() - t, 1),
        }
        attempts.append(rec)
        print(f"[A4] attempt '{rung}': faces={metrics['faces']} tris={metrics['tris']} "
              f"quads={metrics['quads']} ngons={metrics['ngons']} "
              f"nm={metrics['non_manifold_edges']} comp={metrics['components']} "
              f"bbox_min={min(metrics['bbox_per_axis'].values()):.4f} "
              f"verdict={gate.verdict} hard_fail={[c.name for c in gate.hard_failures]} "
              f"soft_fail={[c.name for c in gate.soft_failures]}", flush=True)

        this_score = score(gate, metrics)
        if best is None or this_score < best["score"]:
            if best is not None:
                _remove_object_w(bpy, best["low"])
            best = {"low": low, "gate": gate, "score": this_score, "rec": rec}
        else:
            _remove_object_w(bpy, low)

        if gate.passed:
            print("[A4] gate PASSED — accepting attempt", flush=True)
            break

        rung = next_rung(gate, attempted_rungs)
        if rung in (RUNG_REPORT_FAILED, ""):
            print(f"[A4] retry ladder exhausted (rung={rung or 'none'}); "
                  "shipping best-effort attempt with full history", flush=True)
            break
        if rung == RUNG_DENSER_PROXY:
            print("[A4] ladder recommends a denser (1.5M) proxy — requires a P1 re-run "
                  "with a smaller voxel; not auto-executed in this process. Shipping "
                  "best-effort and recording the recommendation.", flush=True)
            attempted_rungs.append(rung)
            break

        attempted_rungs.append(rung)
        # Apply the rung to the NEXT attempt's params (plan §7 ladder).
        if rung == RUNG_RATIO_REAIM:
            pass  # the ratio search re-converges on its own; just re-run
        elif rung == RUNG_TRIQUAD_TWEAK:
            a3_kwargs["face_threshold_deg"] = 8.0  # more conservative -> fewer risky merges
            a3_kwargs["shape_threshold_deg"] = 8.0
        elif rung == RUNG_FEATURE_PROTECT:
            if a2_kwargs["preserve_features"]:
                a2_kwargs["preserve_features_strength"] += 0.5  # strengthen
            else:
                a2_kwargs["preserve_features"] = True
        elif rung == RUNG_SHRINKWRAP:
            a2_kwargs["shrinkwrap"] = not a2_kwargs["shrinkwrap"]
        print(f"[A4] climbing to rung '{rung}' -> a2={a2_kwargs} a3={a3_kwargs}", flush=True)

    low = best["low"]
    low.name = f"AI_Adaptive_{target_faces}"
    low.data.name = low.name
    report = {
        "selected_rung": best["rec"]["rung"],
        "attempted_rungs": attempted_rungs,
        "attempts": attempts,
        "selected_metrics": best["rec"]["metrics"],
        "gate": best["gate"].to_dict(),
    }
    return {"low": low, "gate": best["gate"], "report": report}


def _uv_reference_baseline(bpy, ref):
    """Score the reference asset's own artist UVs (plan §5 'calibrate first'), plus its
    geometry auto-unwrapped by the same organic pipeline (the *achievable* floor —
    artist seams are not reproducible automatically). Returns
    ``(UVReferenceBaseline, report_dict)``."""
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.blender.organic_unwrap import (
        build_uv_metrics, island_plan_from_seams, read_uvmap, unwrap_organic,
    )
    from uv_agent.geometry.evaluation import evaluate_uv_solution
    from uv_agent.geometry.uv_gate import UVReferenceBaseline
    from uv_agent.planner.island_planner import PlanConstraints
    from uv_agent.planner.organic_seams import crease_seam_edges, organic_seam_edges

    mg = extract_mesh_graph(ref)
    active = ref.data.uv_layers.active
    artist = {}
    if active is not None:
        uv0 = read_uvmap(ref, mg, layer_name=active.name)
        plan0 = island_plan_from_seams(mg, set(), constraints=PlanConstraints())
        ev0 = evaluate_uv_solution(mg, plan0, uv0)
        artist = build_uv_metrics(mg, uv0, ev0)

    # Auto-unwrap the reference geometry the same way we unwrap the AI mesh.
    seams = organic_seam_edges(mg, n_extremities=12) | crease_seam_edges(mg, percentile=87)
    unwrap_organic(ref, seams, margin=0.02)
    uv_a = read_uvmap(ref, mg)
    plan_a = island_plan_from_seams(mg, seams, constraints=PlanConstraints())
    ev_a = evaluate_uv_solution(mg, plan_a, uv_a)
    auto = build_uv_metrics(mg, uv_a, ev_a)

    # Restore the artist layer as active so P6 renders/export are unaffected.
    if active is not None:
        ref.data.uv_layers.active = active

    # Gate stretch against the auto-unwrap floor (achievable), not the artist UVs.
    baseline = UVReferenceBaseline(
        stretch_score=max(artist.get("stretch_score", 0.2), auto["stretch_score"]),
        vt_v_ratio=artist.get("vt_v_ratio", 1.13),
        island_count=int(artist.get("island_count", 1)),
    )
    return baseline, {"artist": artist, "auto_unwrap": auto, "baseline": baseline.to_dict()}


def run_p5_uv(bpy, low, ref, out_dir: str, *, engine: str = "auto") -> dict:
    """Phase P5 — auto-UV. ``engine='transfer'`` (the new default when the reference has
    UVs, UV_TRANSFER_PLAN) transfers the reference's chart LAYOUT onto the mesh — a
    *semantically* artist-like result. ``engine='chart'`` is the no-reference geometric
    chart engine; ``engine='organic'`` is the v1 cut-tree pelt. ``engine='auto'`` picks
    transfer iff the reference carries UVs, else chart. None ships the Smart-UV fallback
    (hard gate); the gate is reported honestly (best-effort on a hard-gate miss)."""
    ref_has_uv = ref is not None and len(ref.data.uv_layers) > 0
    if engine == "auto":
        engine = "transfer" if ref_has_uv else "chart"
    if engine == "transfer":
        return _run_p5_transfer(bpy, low, ref, out_dir)
    if engine == "chart":
        return _run_p5_chart(bpy, low, out_dir)
    return _run_p5_organic(bpy, low, ref, out_dir)


def _run_p5_transfer(bpy, low, ref, out_dir: str) -> dict:
    """Reference-Guided UV Transfer P5 (UV_TRANSFER_PLAN). Fails loud if the reference
    has no UVs (never silently switches engines)."""
    from transfer_uv_agent.pipeline import NoReferenceUVError, run_transfer_uv
    from uv_agent.blender.extract import extract_mesh_graph

    t = time.monotonic()
    if ref is None or len(ref.data.uv_layers) == 0:
        raise NoReferenceUVError(
            "--uv-engine transfer requires a UV'd --reference; none found. "
            "Use --uv-engine chart for the no-reference geometric engine.")
    low_mg = extract_mesh_graph(low)
    ref_mg = extract_mesh_graph(ref)
    res = run_transfer_uv(low, low_mg, ref, ref_mg)
    gate, m, rep = res["gate"], res["metrics"], res["report"]
    shippable = res["shippable"]

    with open(os.path.join(out_dir, "p5_gate.json"), "w", encoding="utf-8") as fh:
        json.dump({"engine": "transfer", "chart_count": res["chart_count"], "metrics": m,
                   "gate": gate.to_dict(), "shippable": shippable, "report": rep,
                   "placements": res["placements"], "adjustments": res["adjustments"],
                   "pack_fallback": res["pack_fallback"], "projection": res["projection"],
                   "seam_count": len(res["seams"])}, fh, indent=2)

    print(f"[P5] transfer UV: charts={m['island_count']} (ref={rep['reference_chart_count']}, "
          f"delta={rep['chart_count_delta']}) raster_overlap={m['raster_overlap_ratio']} "
          f"overlap={m['overlap_ratio']:.5f} texel_var={m.get('texel_density_variance'):.4f} "
          f"stretch={m['stretch_score']:.4f} mean_iou={rep['mean_placement_iou']} "
          f"uncovered_ref={rep['uncovered_count']} local_shrinks={len(res['adjustments'])} | "
          f"gate={gate.verdict} fails={[c.name for c in gate.failures]} "
          f"shippable={shippable} | {time.monotonic() - t:.1f}s", flush=True)
    return {
        "method": "transfer", "engine": "transfer", "fallback_used": False,
        "gate_verdict": gate.verdict, "shippable": shippable,
        "metrics": m, "gate": gate.to_dict(), "report": rep,
        "seam_count": len(res["seams"]), "chart_count": res["chart_count"],
        "placements": res["placements"], "adjustments": res["adjustments"],
    }


def _run_p5_chart(bpy, low, out_dir: str) -> dict:
    """Chart engine P5 (chart-UV plan §6–§8)."""
    from chart_uv_agent.pipeline import run_chart_uv
    from uv_agent.blender.extract import extract_mesh_graph

    t = time.monotonic()
    mg = extract_mesh_graph(low)
    res = run_chart_uv(low, mg)
    gate, m = res["gate"], res["metrics"]
    stuck = res["stuck_charts"]
    shippable = res["shippable"]  # gate.passed OR only convexity_p10 fails with stuck (§5c)

    pre = res.get("metrics_before_correctness", {})
    with open(os.path.join(out_dir, "p5_gate.json"), "w", encoding="utf-8") as fh:
        json.dump({"engine": "chart", "chart_count": res["chart_count"], "metrics": m,
                   "gate": gate.to_dict(), "shippable": shippable, "stuck_charts": stuck,
                   "metrics_before_correctness": pre, "correctness_history": res.get("correctness"),
                   "history": res["history"], "seam_count": len(res["seams"])}, fh, indent=2)

    print(f"[P5] correctness round (raster overlap): "
          f"before raster={pre.get('raster_overlap_ratio')} charts={pre.get('island_count')} "
          f"stretch={pre.get('stretch_score')} -> after raster={m.get('raster_overlap_ratio')} "
          f"charts={m['island_count']} stretch={m['stretch_score']:.3f} packing={m['packing_efficiency']:.3f}",
          flush=True)

    print(f"[P5] chart UV: charts={m['island_count']} stretch={m['stretch_score']:.4f} "
          f"overlap={m['overlap_ratio']:.5f} raster_overlap={m.get('raster_overlap_ratio')} "
          f"packing={m['packing_efficiency']:.4f} convex_p10={m.get('convexity_p10')} "
          f"small={m['small_island_ratio']:.3f} | gate={gate.verdict} "
          f"fails={[c.name for c in gate.failures]} shippable={shippable} "
          f"stuck={len(stuck)} | {time.monotonic() - t:.1f}s", flush=True)
    if stuck:
        print(f"[P5] U1.7 stuck charts ({len(stuck)}, §5c last round, shipped): "
              f"{[(s['size'], s['convexity']) for s in stuck[:6]]}", flush=True)
    return {
        "method": "chart", "engine": "chart", "fallback_used": False,
        "gate_verdict": gate.verdict, "shippable": shippable, "stuck_charts": stuck,
        "metrics": m, "gate": gate.to_dict(), "seam_count": len(res["seams"]),
        "chart_count": res["chart_count"], "history": res["history"],
    }


def _run_p5_organic(bpy, low, ref, out_dir: str) -> dict:
    """Organic cut-tree pelt P5 (UV repair plan, Tracks 1+2) — kept as ``--uv-engine
    organic`` for comparison; never ships the Smart-UV fallback (hard gate)."""
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.blender.organic_unwrap import organic_unwrap_with_refinement
    from uv_agent.geometry.uv_gate import UVGateThresholds
    from uv_agent.planner.organic_seams import classify_seam_strategy, edge_over_threshold_fraction

    t = time.monotonic()
    baseline, base_report = _uv_reference_baseline(bpy, ref)
    print(f"[P5] UV baseline: artist_stretch={base_report['artist'].get('stretch_score')} "
          f"auto_floor={base_report['auto_unwrap']['stretch_score']:.4f} "
          f"vt/v_ref={baseline.vt_v_ratio:.3f}", flush=True)

    mg = extract_mesh_graph(low)
    frac = edge_over_threshold_fraction(mg, 30.0)
    strategy = classify_seam_strategy(mg, angle_threshold=30.0)
    _activate_only(bpy, low)
    res = organic_unwrap_with_refinement(low, mg, baseline=baseline, thresholds=UVGateThresholds())
    gate, m = res["gate"], res["metrics"]

    with open(os.path.join(out_dir, "p5_uv_baseline.json"), "w", encoding="utf-8") as fh:
        json.dump(base_report, fh, indent=2)
    with open(os.path.join(out_dir, "p5_gate.json"), "w", encoding="utf-8") as fh:
        json.dump({"strategy": strategy, "edge_over_30deg": round(frac, 4),
                   "metrics": m, "gate": gate.to_dict(), "history": res["history"],
                   "seam_count": len(res["seams"])}, fh, indent=2)

    print(f"[P5] organic UV: strategy={strategy} islands={m['island_count']} "
          f"overlap={m['overlap_ratio']:.5f} stretch={m['stretch_score']:.4f} "
          f"vt/v={m['vt_v_ratio']:.4f} pack={m['packing_efficiency']:.4f} "
          f"seams={len(res['seams'])} | gate={gate.verdict} "
          f"hard_fail={[c.name for c in gate.hard_failures]} "
          f"soft_fail={[c.name for c in gate.soft_failures]} "
          f"fallback_used=False | {time.monotonic() - t:.1f}s", flush=True)

    return {
        "method": "organic",
        "strategy": strategy,
        "fallback_used": False,
        "gate_verdict": gate.verdict,
        "shippable": gate.passed,
        "metrics": m,
        "gate": gate.to_dict(),
        "baseline": base_report,
        "seam_count": len(res["seams"]),
        "history": res["history"],
    }


def _activate_only(bpy, obj) -> None:
    """Make ``obj`` the sole selected + active object. Uses the op-based deselect
    (robust to a stale None slot in ``view_layer.objects`` left after freeing the
    proxy, which the manual-loop variant trips over)."""
    try:
        bpy.ops.object.select_all(action="DESELECT")
    except RuntimeError:
        for o in bpy.context.view_layer.objects:
            if o is not None:
                o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def run_p6_export(bpy, low, ref, out_dir: str, target_faces: int, reference_path: str) -> dict:
    """Phase P6 — export the mesh (OBJ v/vt/vn + .blend) and render fixed-camera
    silhouettes of the generated mesh AND the reference with ONE shared camera
    (plan §7 render rule / §8 P6). Returns the export paths + a side-by-side table."""
    t = time.monotonic()
    obj_path = os.path.join(out_dir, f"adaptive_t{target_faces}.obj")
    blend_path = os.path.join(out_dir, f"adaptive_t{target_faces}.blend")

    # Fixed shared camera, framed on the reference (stable, same world space as low).
    renders = _render_fixed_camera(bpy, {"generated": low, "reference": ref}, ref, out_dir,
                                   tag=f"adaptive_t{target_faces}")
    # Checker renders (UV_TRANSFER_PLAN calibration round): same checker (scale 40), same
    # fixed camera, generated AND reference, front/side — so UV distortion/correspondence is
    # visible side by side. Same auto-framing-forbidden rule (one shared camera).
    checker_renders = _render_checker(bpy, {"generated": low, "reference": ref}, ref, out_dir,
                                      tag=f"adaptive_t{target_faces}", scale=40.0)

    # Export OBJ with normals + UVs (v/vt/vn), then a .blend of the low-poly alone.
    _activate_only(bpy, low)
    bpy.ops.wm.obj_export(
        filepath=os.path.abspath(obj_path), export_selected_objects=True,
        export_normals=True, export_uv=True, export_materials=False,
        export_triangulated_mesh=False,
    )
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(blend_path), copy=True)

    uv_png = _export_uv_layout(bpy, low, os.path.join(out_dir, f"adaptive_t{target_faces}_uv.png"))
    # Reference artist UV layout, for the side-by-side correspondence review (transfer
    # engine deliverable, UV_TRANSFER_PLAN §6). Best-effort; skipped if the ref has no UVs.
    ref_uv_png = None
    if len(ref.data.uv_layers) > 0:
        ref_uv_png = _export_uv_layout(bpy, ref, os.path.join(out_dir, f"adaptive_t{target_faces}_uv_reference.png"))
    side_by_side_uv = _stitch_side_by_side(bpy, uv_png, ref_uv_png,
                                           os.path.join(out_dir, f"adaptive_t{target_faces}_uv_sidebyside.png"))

    # Side-by-side counts vs the reference.
    table = {
        "generated": {"faces": len(low.data.polygons), "verts": len(low.data.vertices)},
        "reference": {"faces": len(ref.data.polygons), "verts": len(ref.data.vertices)},
    }
    print(f"[P6] exported {obj_path} + {blend_path}; uv_layout={uv_png}; "
          f"renders={list(renders)}; {time.monotonic() - t:.1f}s", flush=True)
    print(f"[P6] side-by-side: generated {table['generated']} vs reference {table['reference']}",
          flush=True)
    return {"obj": obj_path, "blend": blend_path, "uv_layout": uv_png,
            "uv_layout_reference": ref_uv_png, "uv_sidebyside": side_by_side_uv,
            "renders": renders, "checker_renders": checker_renders, "side_by_side": table}


def _apply_checker_uv(bpy, obj, *, scale: float = 40.0, name: str = "AI_Checker"):
    """Attach an emission checker (procedural, no lights needed) mapped through the
    object's ACTIVE UV layer, so a render shows UV stretch/correspondence directly. The
    generated mesh maps through its transferred ``AI_UV`` layer; the reference through its
    artist layer."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emis = nt.nodes.new("ShaderNodeEmission")
    chk = nt.nodes.new("ShaderNodeTexChecker")
    chk.inputs["Scale"].default_value = float(scale)
    uvn = nt.nodes.new("ShaderNodeUVMap")
    active = obj.data.uv_layers.active
    if active is not None:
        uvn.uv_map = active.name
    nt.links.new(uvn.outputs["UV"], chk.inputs["Vector"])
    nt.links.new(chk.outputs["Color"], emis.inputs["Color"])
    nt.links.new(emis.outputs["Emission"], out.inputs["Surface"])
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return mat


def _render_checker(bpy, objects: dict, frame_obj, out_dir: str, *, tag: str,
                    scale: float = 40.0) -> dict:
    """Checker render of each object from the SAME single fixed camera as the silhouettes
    (auto-framing forbidden). Applies the scale-40 checker through each object's UV, renders
    front + side per object. Returns {label_view_checker: path}."""
    import mathutils

    scene = bpy.context.scene
    for eng in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        try:
            scene.render.engine = eng
            break
        except (TypeError, ValueError):
            continue
    scene.render.resolution_x = scene.render.resolution_y = 700
    for _, obj in objects.items():
        _apply_checker_uv(bpy, obj, scale=scale)

    corners = [frame_obj.matrix_world @ mathutils.Vector(c) for c in frame_obj.bound_box]
    centre = sum(corners, mathutils.Vector()) / 8.0
    radius = max((c - centre).length for c in corners) or 1.0
    cam_data = bpy.data.cameras.new("AI_Checker_Cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = radius * 2.2
    cam = bpy.data.objects.new("AI_Checker_Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    scene.camera = cam

    views = {"front": mathutils.Vector((0, -1, 0)), "side": mathutils.Vector((1, 0, 0))}
    out: dict[str, str] = {}
    all_objs = list(objects.values())
    for label, obj in objects.items():
        for other in all_objs:
            other.hide_render = (other is not obj)
        for vname, d in views.items():
            cam.location = centre + d * radius * 3
            cam.rotation_euler = (centre - cam.location).normalized().to_track_quat("-Z", "Z").to_euler()
            path = os.path.join(out_dir, f"{tag}_{label}_{vname}_checker.png")
            scene.render.filepath = path
            bpy.ops.render.render(write_still=True)
            out[f"{label}_{vname}_checker"] = path
    for obj in all_objs:
        obj.hide_render = False
    bpy.data.objects.remove(cam, do_unlink=True)
    bpy.data.cameras.remove(cam_data, do_unlink=True)
    return out


def _stitch_side_by_side(bpy, left_png, right_png, out_path):
    """Composite two UV-layout PNGs horizontally (ours | reference) for the part-
    correspondence review (UV_TRANSFER_PLAN §6). Best-effort via bpy image pixels +
    numpy; returns the path or ``None`` if either input is missing/unloadable."""
    if not left_png or not right_png or not os.path.exists(left_png) or not os.path.exists(right_png):
        return None
    import numpy as np
    try:
        li = bpy.data.images.load(os.path.abspath(left_png))
        ri = bpy.data.images.load(os.path.abspath(right_png))
        lw, lh = li.size
        rw, rh = ri.size
        la = np.array(li.pixels[:]).reshape(lh, lw, 4)
        ra = np.array(ri.pixels[:]).reshape(rh, rw, 4)
        h = max(lh, rh)
        canvas = np.zeros((h, lw + rw, 4), dtype=np.float32)
        canvas[:, :, 3] = 1.0
        canvas[h - lh:, :lw] = la
        canvas[h - rh:, lw:lw + rw] = ra
        out = bpy.data.images.new("AI_UV_SideBySide", width=lw + rw, height=h, alpha=True)
        out.pixels = canvas.reshape(-1).tolist()
        out.filepath_raw = os.path.abspath(out_path)
        out.file_format = "PNG"
        out.save()
        bpy.data.images.remove(li); bpy.data.images.remove(ri); bpy.data.images.remove(out)
        return out_path
    except (RuntimeError, ValueError, AttributeError) as exc:
        print(f"[P6] uv side-by-side stitch skipped ({exc})", flush=True)
        return None


def _export_uv_layout(bpy, low, path: str) -> str | None:
    """Write a UV-layout PNG of the active layer (plan §6 deliverable). Best-effort:
    ``uv.export_layout`` needs an edit-mode UV selection."""
    _activate_only(bpy, low)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="SELECT")
        bpy.ops.uv.export_layout(filepath=os.path.abspath(path), mode="PNG",
                                 size=(1024, 1024), opacity=1.0)
    except (RuntimeError, AttributeError, TypeError) as exc:
        print(f"[P6] uv layout export skipped ({exc})", flush=True)
        path = None
    finally:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError:
            pass
    return path


def _render_fixed_camera(bpy, objects: dict, frame_obj, out_dir: str, *, tag: str) -> dict:
    """Render each object in ``objects`` ({label: obj}) from a SINGLE orthographic
    camera framed on ``frame_obj`` (plan §7: auto-framed per-mesh renders are
    forbidden as evidence). Front + side per object. Returns {label_view: path}."""
    import mathutils

    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = 700

    corners = [frame_obj.matrix_world @ mathutils.Vector(c) for c in frame_obj.bound_box]
    centre = sum(corners, mathutils.Vector()) / 8.0
    radius = max((c - centre).length for c in corners) or 1.0

    cam_data = bpy.data.cameras.new("AI_Fixed_Cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = radius * 2.2
    cam = bpy.data.objects.new("AI_Fixed_Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    scene.camera = cam

    views = {"front": mathutils.Vector((0, -1, 0)), "side": mathutils.Vector((1, 0, 0))}
    out: dict[str, str] = {}
    all_objs = list(objects.values())
    for label, obj in objects.items():
        for other in all_objs:
            other.hide_render = (other is not obj)
        for vname, d in views.items():
            cam.location = centre + d * radius * 3
            cam.rotation_euler = (centre - cam.location).normalized().to_track_quat("-Z", "Z").to_euler()
            path = os.path.join(out_dir, f"{tag}_{label}_{vname}.png")
            scene.render.filepath = path
            bpy.ops.render.render(write_still=True)
            out[f"{label}_{vname}"] = path
    for obj in all_objs:
        obj.hide_render = False
    bpy.data.objects.remove(cam, do_unlink=True)
    bpy.data.cameras.remove(cam_data, do_unlink=True)
    return out


def _remove_object_w(bpy, obj) -> None:
    """Remove an object and its now-orphan mesh (worker-local helper)."""
    mesh = obj.data if getattr(obj, "type", None) == "MESH" else None
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    except (RuntimeError, ReferenceError):
        pass


def _adaptive_markdown(report: dict) -> str:
    g = report["generation"]
    gate = report["gate"] if "gate" in report else g.get("gate", {})
    sel = g.get("selected_metrics", {})
    p5 = report.get("p5_uv", {})
    p6 = report.get("p6_export", {})
    bbox = sel.get("bbox_per_axis", {})
    lines = [
        f"# Adaptive low-poly — target {report['target_faces']:,} faces",
        "",
        f"- input: `{report['input']}` | reference: `{report['reference']}`",
        f"- **verdict: {report['verdict'].upper()}** (gate_passed={report['gate_passed']})",
        f"- total: {report['timings_s']['total']}s | peak RSS {report['peak_rss_gb']} GB",
        f"- selected rung: `{g.get('selected_rung')}` | rungs climbed: {g.get('attempted_rungs')}",
        "",
        "## Selected mesh",
        f"- faces: {sel.get('faces'):,} (tris {sel.get('tris')}, quads {sel.get('quads')}, "
        f"n-gons {sel.get('ngons')})",
        f"- verts: {sel.get('vertex_count'):,} | non-manifold: {sel.get('non_manifold_edges')} | "
        f"components: {sel.get('components')}",
        f"- bbox coverage per axis: {bbox} (worst {min(bbox.values()) if bbox else 'n/a'})",
        f"- proxy→low: {sel.get('proxy_to_low')}",
        f"- low→proxy: {sel.get('low_to_proxy')}",
        "",
        "## Reference baseline (vs proxy)",
        f"- {report['baseline']}",
        "",
        "## Gate checks",
    ]
    for c in gate.get("checks", []):
        flag = "✅" if c["passed"] else "❌"
        lines.append(f"- {flag} `{c['kind']}` **{c['name']}**: {c['detail']}")
    lines += [
        "",
        "## P5 — UV (organic, Tracks 1+2)",
        f"- method: {p5.get('method')} | strategy: {p5.get('strategy')} | "
        f"fallback_used: {p5.get('fallback_used')} | shippable: {p5.get('shippable')}",
        f"- islands: {(p5.get('metrics') or {}).get('island_count')} | "
        f"overlap: {(p5.get('metrics') or {}).get('overlap_ratio')} | "
        f"stretch: {(p5.get('metrics') or {}).get('stretch_score')} | "
        f"vt/v: {(p5.get('metrics') or {}).get('vt_v_ratio')} | seams: {p5.get('seam_count')}",
        f"- gate: {p5.get('gate_verdict')} | hard_fail: {(p5.get('gate') or {}).get('hard_failures')} "
        f"| soft_fail: {(p5.get('gate') or {}).get('soft_failures')}",
        "",
        "## P6 — export",
        f"- OBJ: `{p6.get('obj')}` | blend: `{p6.get('blend')}`",
        f"- side-by-side: {p6.get('side_by_side')}",
        f"- renders: {list((p6.get('renders') or {}).keys())}",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
