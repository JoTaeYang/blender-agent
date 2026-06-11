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
    p5 = run_p5_uv(bpy, low, out_dir)

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


def run_p5_uv(bpy, low, out_dir: str) -> dict:
    """Phase P5 — auto-UV the adaptive mesh via uv_agent (mock provider; Blender-only,
    no network — plan §8/P5). Seams → unwrap → pack, then write the loop UVs and the
    UV evaluation (no overlaps / stretch band / margins)."""
    from uv_agent.agent.llm import get_provider
    from uv_agent.agent.pipeline import UVAgentPipeline
    from uv_agent.blender.apply import apply_uv_coordinates
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.planner.island_planner import PlanConstraints

    t = time.monotonic()
    constraints = PlanConstraints(padding_px=8, texture_size_px=1024, max_overlap_ratio=0.0)
    pipeline = UVAgentPipeline(get_provider("mock"), max_iterations=4, angle_threshold=30.0)
    mg = extract_mesh_graph(low)
    result = pipeline.run(mg, "adaptive low-poly automatic unwrap", constraints=constraints)
    written = apply_uv_coordinates(low, result.solution, seam_edge_ids=result.plan.seam_edge_ids)

    with open(os.path.join(out_dir, "p5_solution.json"), "w", encoding="utf-8") as fh:
        json.dump(result.solution.to_dict(), fh, indent=2)
    with open(os.path.join(out_dir, "p5_evaluation.json"), "w", encoding="utf-8") as fh:
        json.dump(result.to_dict()["evaluation"], fh, indent=2)

    ev = result.evaluation
    method = "uv_agent"
    fallback = None
    # Smart-UV fallback (plan §8/§9 risk row): the planner's seam/unwrap can leave
    # overlaps on a highly organic tri/quad mesh, which the metrics gate (rightly)
    # rejects. Rather than ship overlapping UVs, fall back to Blender's Smart UV
    # Project, which is non-overlapping by construction. The planner result is still
    # recorded so the fallback is auditable, never silent.
    if ev.status != "accepted":
        fallback = _smart_uv_fallback(bpy, low)
        method = "smart_uv_fallback"

    print(f"[P5] UV: planner status={ev.status} ({written} loop UVs, "
          f"{len(result.plan.seam_edge_ids)} seams); method={method}"
          + (f"; fallback={fallback}" if fallback else "")
          + f"; {time.monotonic() - t:.1f}s", flush=True)
    return {
        "method": method,
        "planner_status": ev.status,
        "fallback": fallback,
        "loop_uvs_written": written,
        "seam_edges": len(result.plan.seam_edge_ids),
        "planner_evaluation": result.to_dict()["evaluation"],
    }


def _smart_uv_fallback(bpy, low) -> str:
    """Blender Smart UV Project on ``low`` — a non-overlapping unwrap fallback when the
    uv_agent planner doesn't pass the metrics gate (plan §8 P5). Replaces the active
    UV layer in place; the P6 OBJ export then writes these UVs as ``vt``."""
    import math

    _activate_only(bpy, low)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.02)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    return "smart_project(angle_limit=66deg, island_margin=0.02)"


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

    # Export OBJ with normals + UVs (v/vt/vn), then a .blend of the low-poly alone.
    _activate_only(bpy, low)
    bpy.ops.wm.obj_export(
        filepath=os.path.abspath(obj_path), export_selected_objects=True,
        export_normals=True, export_uv=True, export_materials=False,
        export_triangulated_mesh=False,
    )
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(blend_path), copy=True)

    # Side-by-side counts vs the reference.
    table = {
        "generated": {"faces": len(low.data.polygons), "verts": len(low.data.vertices)},
        "reference": {"faces": len(ref.data.polygons), "verts": len(ref.data.vertices)},
    }
    print(f"[P6] exported {obj_path} + {blend_path}; renders={list(renders)}; "
          f"{time.monotonic() - t:.1f}s", flush=True)
    print(f"[P6] side-by-side: generated {table['generated']} vs reference {table['reference']}",
          flush=True)
    return {"obj": obj_path, "blend": blend_path, "renders": renders, "side_by_side": table}


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
        "## P5 — UV",
        f"- method: {p5.get('method')} (planner status: {p5.get('planner_status')}"
        + (f", fallback: {p5.get('fallback')}" if p5.get('fallback') else "")
        + f") | loop UVs: {p5.get('loop_uvs_written')} | seams: {p5.get('seam_edges')}",
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
