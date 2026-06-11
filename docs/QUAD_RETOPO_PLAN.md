# Quad Retopology + Auto-UV Agent — Implementation Plan

> **SUPERSEDED AS DEFAULT (2026-06-11):** the user redefined the goal as adaptive,
> silhouette-first tri/quad decimation — see `docs/ADAPTIVE_LOWPOLY_PLAN.md` (v2),
> which is now the active plan. This QuadriFlow path is kept as a dormant
> `--mode quad` (tested, frozen, not extended). Rationale: at the 2,900 budget
> QuadriFlow structurally truncated thin extremities (P2 coverage experiment).

> Audience: implementation agent. This document is self-contained; read it fully before
> writing code. It supersedes `docs/DECIMATION_MASTER_PLAN.ko.md` as the primary path to
> low-poly output. The existing `retopo_agent` decimation pipeline is considered a
> **failed approach** for the target deliverable and must NOT be the final output path.

## 1. Mission

Build a Blender-only, headless pipeline that converts a multi-million-face sculpt into a
**pure-quad, natural-edge-flow, UV-unwrapped** low-poly mesh.

Acceptance scenario (the single source of truth for "done"):

```
input : sample/humanstatue.obj        (ZBrush export, 24,580,077 verts / 24,893,101 faces, no UVs, 1.86 GB)
output: a generated humanstatue_low   comparable in quality to sample/humanstatue_low.obj
```

The reference output `sample/humanstatue_low.obj` (do not overwrite it — it is the
ground-truth target, produced by an artist/ZRemesher-class tool) has:

| metric | value |
|---|---|
| verts | 3,191 |
| faces | 5,850 (5,799 **triangles** + 51 quads, 0 n-gons) — verified P0, see note |
| UVs | yes (3,612 `vt`) |
| normals | yes (3,523 `vn`) |
| coordinate space | **identical to the high-poly** (bbox y: 17.0–526.0 vs 17.5–526.0) |

Both meshes live in the same world space. This is a major asset: every generated result
can be quantitatively compared against both the high-poly *and* the reference low-poly.

> **P0 correction (2026-06-10):** the reference `humanstatue_low.obj` is **triangle-
> dominant** (5,799 triangles + 51 quads, verified by raw `f`-line count), not the pure-
> quad mesh the original table claimed. This does **not** weaken our deliverable — §2.1
> still requires 100% quads in *our* output, which QuadriFlow delivers and the reference
> does not. But it means the reference's *quad ratio* is NOT a flow-quality benchmark for
> P4; only its **shape-distance / normal-deviation** metrics (§10) are usable as a
> baseline. Our generated mesh is strictly better-topologized than the reference.

> **Budget decision (2026-06-10, user-confirmed — option A):** because the reference is a
> triangle mesh, "match its face count with quads" would double the real geometric budget
> (a 5,850-quad mesh ≈ 5,850 verts ≈ 11,700 tris vs the reference's 3,191 verts / 5,799
> tris). The confirmed target semantics are **asset-budget parity, not face-count parity**:
>
> **`T_goal ≈ 2,900 quads`** (≈ 2,900 verts ≈ 5,800 triangulated tris ≈ reference budget).
>
> All face-count targets below (§5, §8, §10, §12, §15) use this value. `--target-faces`
> stays a CLI parameter, so face-count parity (5,850 quads) remains one flag away.
> Note: the P0 spike validated QuadriFlow at the 5.8k target; the 2.9k target is expected
> to hold (P0 silhouettes had headroom) but must be confirmed on the first P2 run —
> instability at the lower target is exactly what the seed-retry + two-stage ladder
> (§8.4–8.5, §10) exists for.

## 2. Hard requirements

1. **Pure quads.** 100% quad faces in the output. 0 triangles, 0 n-gons. (The reference
   is triangle-dominant — 5,799 tris + 51 quads, see §1 P0 correction; we aim much
   stricter — QuadriFlow natively emits pure quads.)
2. **Natural edge flow.** Edge loops must follow the form (limbs, torso, face), not a
   random tessellation. QuadriFlow-class field-aligned remeshing, not decimation.
3. **Automatic UVs.** The exported OBJ must contain `vt` (and `vn`). Reuse the existing
   `uv_agent` engine for seam planning / unwrap / packing.
4. **Blender only.** Blender 5.0.1 at `/Applications/Blender.app/Contents/MacOS/Blender`,
   headless (`--background --python ...`). No Instant Meshes, no ZBrush, no external
   remeshing binaries.
5. **Scale.** Must survive a 24.9M-face / 1.86 GB OBJ on a 36 GB RAM machine.

## 3. Why the previous approach failed (read before coding)

The existing `retopo_agent` is built around `Decimate (COLLAPSE)` ratio search
(`retopo_agent/blender/decimate.py`, plan in `docs/DECIMATION_MASTER_PLAN.ko.md`).
Findings from that effort:

- Collapse output is a **triangle soup** — categorically wrong vs. a quad-flow target.
- On `sample/anchor.obj` (9.8M faces) Collapse plateaued at 8,008 faces even at
  `ratio=0` due to non-manifold/boundary constraints. Target tracking failed.
- No UV stage existed at all.
- No ingestion strategy for ~25M-face inputs.

**Strategic change:** decimation is demoted to an internal *proxy generation* step only.
The output path is **voxel/manifold preprocess → QuadriFlow quad remesh → shrinkwrap
re-projection → uv_agent unwrap → export**.

## 4. What to reuse vs. discard

Reuse (verify each still works before depending on it):

- `retopo_agent/geometry/validate.py` — topology validator (quad ratio, non-manifold, face-count band).
- `retopo_agent/geometry/shape_eval.py` + `retopo_agent/blender/shape.py` — surface-distance / normal-deviation evaluation (BVH-based in Blender).
- `retopo_agent/geometry/target_search.py` — target-count control-loop concepts (QuadriFlow `target_faces` is approximate; we need the search loop).
- `retopo_agent/blender/diagnosis.py` — topology diagnosis (components, non-manifold, holes).
- `worker/run_retopo_job.py` — headless worker harness pattern (CLI args → job → JSON report). Add a new mode rather than rewriting from scratch, or create a sibling `worker/run_quad_retopo_job.py` if the existing file is too entangled.
- `uv_agent` (entire package) — seam planning, unwrap, packing, OBJ export with UVs. Entry pattern: `worker/run_uv_job.py`.
- `uv_agent/io/obj_loader.py` — lightweight OBJ stat/loading for validation outside Blender.

Discard / demote:

- `retopo_agent/blender/decimate.py` ratio-search as a *final output* path → keep only as proxy builder.
- `retopo_agent/geometry/decimate.py` vertex-clustering decimation → not on the output path.
- The DM1–DM8 phases of `DECIMATION_MASTER_PLAN.ko.md` → frozen, do not extend.

## 5. Pipeline overview

```
sample/humanstatue.obj (24.9M faces)
  P0  Spike test (manual, throwaway)        — validate QuadriFlow feasibility FIRST
  P1  Ingest + diagnosis + PROXY build      — import, voxel-remesh/decimate to ~500k–1M tris,
                                              save proxy .blend; free the original from RAM
  P2  QuadriFlow quad remesh                — target ≈ 2,900 quads (§1 budget decision),
                                              control loop on target_faces,
                                              preserve_sharp/boundary flags, pure-quad assert
  P3  Shape recovery                        — shrinkwrap (project) onto proxy + corrective pass,
                                              re-measure surface distance
  P4  Quality gate + retry ladder           — thresholds derived from the REFERENCE low-poly
  P5  Auto UV (uv_agent)                    — seams → unwrap → pack; smooth normals
  P6  Export + final acceptance             — OBJ with v/vt/vn; compare vs reference
  P7  Tests + docs                          — unit + headless integration tests
```

All Blender steps run headless:
`/Applications/Blender.app/Contents/MacOS/Blender --background --python <script> -- <args>`

---

## 6. Phase P0 — Spike test (do this before building anything)

Goal: prove the core hypothesis cheaply. One throwaway script, no architecture.

1. Import `sample/humanstatue.obj` with the new C++ importer
   (`bpy.ops.wm.obj_import`). Expect minutes, not hours; monitor RSS.
   If import OOMs, fall back to importing in a fresh Blender with
   `--factory-startup` and immediately apply step 2.
2. Voxel remesh the object (Object ▸ Remesh modifier or `bpy.ops.object.voxel_remesh`,
   voxel size chosen so the result is ~1–2M faces; start with
   `voxel_size = bbox_diagonal / 600` and adjust). Purpose: guaranteed-manifold,
   watertight input for QuadriFlow, and a big memory reduction.
   - Alternative if voxel remesh is too slow/heavy at this density: Decimate(Collapse)
     `ratio ≈ 0.04` first (24.9M → ~1M), then voxel remesh the result.
3. Run `bpy.ops.object.quadriflow_remesh(target_faces=5800, use_mesh_symmetry=False)`.
4. Shrinkwrap the result onto the proxy, apply.
5. Export OBJ and report: face count, quad ratio, wall time per step, peak memory,
   and 2–3 silhouette renders vs the reference low-poly.

**Decision gate:** if QuadriFlow at ~5.8k faces produces broken output (it can be
unstable at very low targets), the fallback *within Blender* is:
QuadriFlow to a higher count (e.g. 20–25k) → `Un-Subdivide`-style reduction is NOT
acceptable (breaks flow) → instead re-run QuadriFlow on the 25k mesh down to 5.8k
(two-stage QuadriFlow), which is typically more stable than one 1M→5.8k jump.
Record what worked in the spike report before proceeding.

### P0 spike results (2026-06-10) — HYPOTHESIS PROVEN ✅

Script: `scripts/spike_quad_retopo.py` (throwaway). Report: `out/spike_p0/spike_report.json`.
Single-stage path (no two-stage fallback needed) on `sample/humanstatue.obj`:

| step | wall | peak RSS | output |
|---|---|---|---|
| import (`wm.obj_import`) | 13.5 s | 9.5 GB | 24,893,101 faces |
| voxel remesh **(direct, voxel_size = diag/600 = 0.974)** | 18.3 s | 11.3 GB | 383,362 faces, 100% quad, manifold |
| QuadriFlow (`target_faces=5800`, no symmetry) | 7.2 s | 11.7 GB | **5,401 faces, 100% quad, in band** |
| shrinkwrap (NEAREST) + export + 3 renders | < 1 s | 11.7 GB | `out/spike_p0/spike_quad.obj` |
| **total** | **39 s** | **11.7 GB** | feasible = **true** |

Findings carried into P1+:
- **Single-stage QuadriFlow is viable** at 5.8k target → two-stage is a P4-ladder fallback, not the default.
- **voxel_size = diag/600 undershoots the proxy band**: it produced **383k** faces, below the
  500k–1.5M target. For P1, raise the divisor (≈ diag/950–1000) or binary-search voxel size
  to land at ~1M, so flow-quality judgments aren't made on an under-resolved proxy.
- Voxel remesh outputs **pure quads**, not triangles — the "triangle proxy" wording in §7 P1.3
  is cosmetic; QuadriFlow accepts the quad proxy fine.
- Decimate(Collapse)-first is an **OOM-only fallback**, not a speed step (see §7 P1.3 note).

## 7. Phase P1 — Scalable ingest + proxy

Deliverable: `retopo_agent/blender/proxy.py` + a `prepare_proxy` job step.

1. **Import**: `bpy.ops.wm.obj_import(filepath=...)`. Log import time + `len(mesh.polygons)`.
2. **Diagnosis** (reuse `diagnosis.py`): components, non-manifold edges, boundary edges,
   degenerate faces. Store in the job report. The ZBrush export is quad-dominant and
   likely near-manifold but possibly multi-component — voxel remesh will fuse
   components, which is acceptable for a statue (document this behavior in the report).
3. **Proxy build**: produce a triangle proxy of **500k–1.5M faces** that is
   *strictly manifold* (this is QuadriFlow's input contract):
   - **primary: voxel remesh applied DIRECTLY to the full-resolution import** at the
     computed voxel size (binary-search voxel size to land in the face band — reuse
     the `target_search.py` loop pattern). Voxel remesh is multithreaded and grid-based,
     so it scales to 24.9M faces; it also guarantees the watertight manifold QuadriFlow
     needs.
   - **fallback ONLY on OOM: Decimate(Collapse) to ~2M first**, then voxel remesh the
     result. Do NOT use Collapse as a speed optimization — it is the opposite.
   > P0 finding (2026-06-10): a `Decimate(Collapse)` pre-step on the 24.9M-face import
   > did **not finish in 42+ min** (single-threaded), while the raw OBJ *import* took
   > 17.7 s and peaked at 7.6 GB. Collapse-first is therefore demoted to an OOM-only
   > fallback and voxel-direct is the primary path. See `out/spike_p0/spike_report.json`
   > (+ `decimate_first_aborted.json`) for the evidence.
4. **Persist**: save `out/<job>/proxy.blend` containing ONLY the proxy (delete the
   original object and `bpy.ops.outliner.orphans_purge` before saving). All later
   phases open `proxy.blend`, never the 1.86 GB OBJ again.
5. **Proxy fidelity check**: sample ~10k points, measure proxy↔original max/mean
   distance *before* discarding the original (BVH from `blender/shape.py`). Record it;
   this bounds the error budget of everything downstream.

Memory rules: one object at a time; apply modifiers instead of stacking; purge orphans
after every destructive step; never keep both original and proxy in scene after P1.

### P1 results (2026-06-10) — IMPLEMENTED ✅

Modules: `retopo_agent/blender/proxy.py` + `worker/run_quad_retopo_job.py` (P1 phase).
Pure tests: `tests/test_proxy_plan.py` (green). Run on `sample/humanstatue.obj`,
`--proxy-faces 1000000` → `out/quad_p1/` (`p1_report.json`, `p1_report.md`, `proxy.blend`):

| step | wall | output |
|---|---|---|
| import | 9.8 s | 24,893,101 faces, area 312,325, diag 584.25, 519 degenerate |
| source diagnosis (bmesh) | 39.3 s | **52 components** (41 tiny, smallest 1 face, largest only **19.3%**), **4,106 non-manifold**, 4,106 boundary |
| proxy build (voxel-direct, **2 probes**) | 21.8 s | **999,328 faces** (target 1.0M, err 0.07%, band=accepted), voxel 0.604 |
| manifold check | — | non-manifold 0, boundary 0, **is_manifold=True**, components **2** |
| fidelity (original→proxy) | 0.6 s | mean 2.87 (0.49%), max 35.9 (6.15%), normal 24.4°; **dist: p50 0.014, p90 8.84, p99 26.1** |
| **total** | **74.7 s** | peak **15.0 GB**, `proxy.blend` = 36 MB (from 1.86 GB) |

Findings carried into P2+:
- **Seeded voxel search converges in 2 probes** (seed 0.559→1.17M, then 0.604→999k); the
  area/voxel² seed (`estimate_initial_voxel_size`) made the binary search nearly free —
  no wasted 24.9M-face remesh probes. The §6 P0 worry about diag/600 undershooting is
  resolved: the seed targets the band directly.
- **The source is NOT one clean mesh** (§7.2 diagnosis backfill): **52 components**, 41 of
  them tiny detached fragments (smallest = 1 face, 86 faces total), and the *largest shell
  is only 19.3% of the faces* — i.e. the statue is many overlapping/nested ZBrush shells,
  plus 4,106 non-manifold and 4,106 boundary edges. This is exactly why we voxel-remesh:
  it fused all of that into a clean **2-component watertight manifold**. The tiny
  fragments were dropped (negligible). This is the documented multi-component behaviour
  §7.2 anticipated, now quantified.
- **Proxy has 2 components, not 1** — voxel remesh did not fully fuse the statue (the
  trident/staff is spatially separate from the body); both shells are closed manifolds, so
  QuadriFlow's contract holds. **P2's assert uses "≤ proxy component count" (= 2)**, not a
  hard "single component" (plan §8.3 allows this).
- **Fidelity mean 2.87 (4.75× voxel) is a HEAVY TAIL, not broad smoothing** — the
  distribution settles it: **p50 = 0.014** (the outer surface is captured near-perfectly),
  while p90 = 8.8, p99 = 26.1, max = 35.9. So ~10% of *original* sample points are far from
  the proxy — those sit on the **internal / overlapping / detached shells** (52 components!)
  and the thinnest features, which the outer watertight proxy correctly omits. Benign for a
  low-poly silhouette, but **P4 must still eyeball the trident prongs / fingers** (see the
  §10 watch item). The normal deviation 24.4° is voxel faceting, removed by P3's
  relax-then-reproject. Error-budget bound, recorded not gated; §10 thresholds are tuned
  for final-low-poly-vs-high-poly, so a 1M proxy scoring "retry" against them is expected.

## 8. Phase P2 — QuadriFlow remesh with target control loop

Deliverable: `retopo_agent/blender/quadremesh.py`.

1. Open `proxy.blend`, run
   `bpy.ops.object.quadriflow_remesh(target_faces=N, use_preserve_sharp=..., use_preserve_boundary=..., seed=...)`.
2. **Control loop** (QuadriFlow's actual output count deviates from `target_faces`):
   accept if `faces ∈ [0.85·T_goal, 1.15·T_goal]` with `T_goal = 2,900` (§1 budget
   decision; parameterized via `--target-faces`);
   otherwise adjust `target_faces` proportionally and retry (max 4 iterations).
   Reuse the loop structure from `geometry/target_search.py`.
3. **Hard asserts after every attempt** (reuse `geometry/validate.py`):
   - 100% quads (`len(p.vertices) == 4` for all polygons) — fail the attempt otherwise;
   - 0 non-manifold edges;
   - single connected component (or ≤ the component count of the proxy).
   - **Directional coverage (DIRECTION MATTERS).** Measure **proxy→quad** distance —
     sample the *proxy* surface, nearest point on the *quad* (BVH on the quad) — and gate
     its max / p99. The P1 fidelity measures the opposite direction (quad→proxy), which is
     structurally blind to geometry the quad *dropped* (a missing trident prong is far from
     the quad but every quad point is still near the proxy). A cheap per-axis bbox-extent
     coverage (≥99%) runs first as an O(verts) screen — a thin feature that defines an
     extreme shrinks the bbox when lost. Thresholds calibrated from the P2 coverage
     experiment (`scripts/spike_p2_coverage.py`), implemented in
     `retopo_agent/blender/quadremesh.py::coverage_report`.
4. **Seed retry**: QuadriFlow is seed-sensitive at low targets. On a failed assert,
   retry with `seed += 1` (up to 3 seeds) before escalating to the retry ladder (P4).
5. Two-stage option from the P0 decision gate must be implemented as a flag
   (`--two-stage`): proxy → ~20k quads → T_goal. (P0 proved single-stage at 5.8k; the
   lower 2.9k target raises instability odds, so keep this fallback ready.)

### P2 results (2026-06-10) — IMPLEMENTED ✅, single-stage stable at 2.9k

Modules: `retopo_agent/blender/quadremesh.py` + `worker/run_quad_retopo_job.py` (P2 phase,
resumes from `proxy.blend`). Pure tests: `tests/test_quadremesh_plan.py` (green). Run:
`--from-phase P2 --target-faces 2900` on `out/quad_p1/proxy.blend` → `quad.blend` +
`p2_report.json`:

| metric | value |
|---|---|
| faces | **3,270** (target 2,900, err 12.8%, band=accepted) |
| quad ratio | **1.0** (tris 0, n-gons 0 — pure quad) |
| non-manifold edges | **0** |
| components | **1** (QuadriFlow fused the proxy's 2 shells; bound was 2) |
| seed / attempts | seed 0, 1 attempt, target search 2 iterations | 
| wall | 30 s (incl. open proxy.blend) |

Findings:
- **The §1 budget worry is resolved: 2,900 quads is stable single-stage** — no seed bump,
  no two-stage needed, pure-quad + manifold on the first attempt. `--two-stage` is
  implemented and kept as a P4-ladder fallback, but is not the default path.
- The control loop re-aimed once (request 2,900 → 3,507) and landed at 3,270 (+12.8%,
  inside the ±15% accept band). If tighter parity is wanted, lower `--target-faces`
  slightly; the budget-parity goal (≈ reference's 3,191 verts) is already met
  (3,270-quad mesh ≈ 3,272 verts).
- `quad.blend` persists BOTH `AI_Quad` and `AI_Proxy` so P3 has its shrinkwrap target.

### P2 directional-coverage experiment (2026-06-10) — cause isolation

`scripts/spike_p2_coverage.py` (sweep) + `scripts/spike_p2_localize.py` (localization),
measuring **proxy→quad** distance (the direction the P1 quad→proxy fidelity is blind to)
and per-axis bbox coverage. Single-shot QuadriFlow per config (no control loop), vs the
1M proxy:

| config | faces | bbox min-axis | proxy→quad max (ratio) | p99 (ratio) |
|---|---|---|---|---|
| 2,900 (seeds 0–3) | ~2,400 | **0.80** | **86 (0.148)** | 70 (0.12) |
| 2,900 preserve_sharp | 2,930 | **24.4 (EXPLODED)** | — | — |
| 6,000 | 5,086 | 0.969 | 54 (0.092) | 17 (0.030) |
| **10,000** | 8,957 | **0.990** | **8.0 (0.014)** | 2.6 (0.004) |
| 20,000 | 17,935 | 0.998 | 6.7 (0.011) | 1.5 (0.003) |
| two-stage 10k→2,900 | 2,418 | **0.80** | **86 (0.147)** | 67 (0.114) |

**Findings (the trident is NOT the problem):**
- **Renders show the trident + full silhouette intact at 2,900** (`out/quad_p1/p2_exp/`). The
  coverage gap is real but is **not a dropped feature**. Localization: the far-from-quad
  proxy points (3.2% of samples, max 86 u) form one cluster spanning the **entire y<0 side,
  full height** (bbox [176,112,453]) — i.e. **deep robe/cape concavity + one-sided depth that
  low-target QuadriFlow bridges/pulls inward**, not a missing prong.
- The proxy's "2 components" is comp0 = 999,318 faces (the figure) + **comp1 = 10 faces** (a
  2×2×3-unit speck). The component bound is effectively 1; the speck is negligible.
- **Coverage is resolution-bound and monotone**: bbox 0.80→0.97→0.99→1.00 and max-ratio
  0.148→0.092→0.014→0.011 as target goes 2.9k→6k→10k→20k. Full coverage arrives at **~10k**.
- **Seed-insensitive** (seeds 0–3 identical) and **two-stage does NOT help** (10k→2.9k loses
  the depth again at the 2.9k step — same 0.80 / max 86 as single-stage). `preserve_sharp` at
  2.9k **explodes** (flyaway geometry, bbox 24×) and is excluded; the bbox upper-bound guard
  (`COVERAGE_BBOX_MAX_RATIO`) now rejects it.

**Decision — vanilla 2,900 is NOT structurally impossible; the strict coverage gate moves to
P3.** Because raw QuadriFlow inherently pulls inward at low targets (proven monotone above)
and the lost quantity is concavity *depth* (recoverable by re-projection, and not holdable at
~3.2k verts by the artist reference either), gating P2's raw output on proxy→quad max/p99
would falsely reject acceptable 2.9k meshes. Therefore:
- **P2 hard-asserts**: pure-quad, 0 non-manifold, component-bounded, **+ bbox explosion guard**
  (the only coverage check valid pre-shrinkwrap). Full directional coverage is **recorded**.
- **P3/P4 hard-gate**: the strict proxy→quad max/p99 + bbox-min coverage, measured **after**
  shrinkwrap re-projection. Thresholds (`COVERAGE_MAX_RATIO=0.05`, `P99_RATIO=0.025`,
  `BBOX_MIN_RATIO=0.99`) are calibrated so post-projection 2.9k must reach ~10k-quality
  coverage; if P3 can't close the gap, **then** escalate (denser allocation / per-part remesh,
  or revisit the §1 budget) — to be confirmed by the first P3 run.

## 9. Phase P3 — Shape recovery (re-projection)

Deliverable: `retopo_agent/blender/project.py` (or extend existing shrinkwrap code in
`blender/retopo.py` — read it first; a shrinkwrap adapter already exists there).

1. Add a **Shrinkwrap** modifier on the quad mesh, target = proxy,
   `wrap_method='PROJECT'` with `use_negative_direction=True` along normals; compare
   against `wrap_method='NEAREST_SURFACEPOINT'` and keep the better (lower shape error).
2. Apply, then one light **Corrective Smooth** (or Taubin relax reusing
   `geometry/quadflow.py` relax) followed by a second shrinkwrap `NEAREST_SURFACEPOINT`
   pass — relax-then-reproject removes QuadriFlow's faceting without losing volume.
3. Measure surface distance + normal deviation vs the proxy (reuse
   `blender/shape.py`). Store before/after numbers in the report.

## 10. Phase P4 — Quality gate + retry ladder

Deliverable: `retopo_agent/geometry/quad_gate.py` + ladder wiring in the worker.

**Thresholds are derived from the reference, not invented.** First, run the shape
evaluator on `sample/humanstatue_low.obj` vs the proxy (they share world space) and
record its mean/p95 surface distance and normal deviation. The generated mesh passes if:

| check | threshold |
|---|---|
| quad ratio | == 1.0 (hard) |
| triangles / n-gons | 0 (hard) |
| non-manifold edges | 0 (hard) |
| face count | within ±15% of T_goal = 2,900 (parameter, §1 budget decision) |
| vertex count | ≤ ~1.15 × reference's 3,191 (sanity check on budget parity) |
| mean surface distance | ≤ 1.5 × reference's mean distance |
| p95 surface distance | ≤ 1.5 × reference's p95 |
| normal deviation (mean) | ≤ 1.5 × reference's |
| silhouette renders | generated for human review (front/side/¾), not auto-gated |

> **P1 watch item for P4 silhouette review (2026-06-10):** the proxy↔original fidelity
> mean distance was **2.87 units ≈ 4.7× the voxel size** (a pure voxel-approximation
> error would sit near ~0.5× voxel). That gap is most likely ZBrush sub-voxel
> high-frequency detail (cloth folds, skin) being flattened — *harmless*, since a ~2.9k
> low-poly can't carry that detail anyway. But it could instead be **thin-feature loss**
> wide enough to read in the silhouette. The P1 report now records the distance
> *distribution* (p50/p90/p99/max) to disambiguate: a broad mean with a modest p99 ⇒
> benign smoothing; a low p50 with a huge p99/max ⇒ localized loss. **The P4 silhouette
> review MUST explicitly zoom on the trident/staff prongs and the fingers** (the thinnest
> features, where the 0.6-unit voxel and the source's separate trident component are most
> likely to have dropped or fused geometry) and compare them against the reference before
> passing the gate.

Retry ladder (cheapest knob first), max ~8 total attempts, every attempt logged to the
JSON report with its failing metric:

1. QuadriFlow seed bump (P2.4)
2. `target_faces` re-aim (P2.2)
3. toggle `use_preserve_sharp` / `use_preserve_boundary`
4. two-stage QuadriFlow (25k → 5.8k)
5. denser proxy (e.g. 1.5M instead of 500k) — re-run P1.3 with smaller voxel
6. final: report `failed` with the best attempt kept + full metric history. Never
   silently return a non-pure-quad mesh.

## 11. Phase P5 — Automatic UVs (uv_agent integration)

Deliverable: glue module `retopo_agent/uv_bridge.py` + worker step.

1. Read `worker/run_uv_job.py` and `uv_agent/agent/pipeline.py` first to learn the
   expected input contract (it consumes a mesh in-scene or an OBJ — follow whichever
   `run_uv_job.py` does today).
2. Feed the accepted quad mesh from P4 into the uv_agent pipeline:
   seam planning (`uv_agent/planner`) → unwrap/relax (`uv_agent/geometry`) → packing
   (`uv_agent/geometry/packing.py`) → apply (`uv_agent/blender/apply.py`).
3. For a humanoid statue the seam planner must produce closed seam loops (limbs cut,
   head/torso separated or a single shell with relief cuts). If the existing planner's
   heuristics underperform on the 5.8k quad mesh, fall back to Blender Smart UV only as
   a *diagnostic baseline*, not as the shipped path — fix the planner instead.
4. UV acceptance metrics (reuse `uv_agent/geometry/evaluation.py`): no overlapping
   islands, stretch within the evaluator's accepted band, island margin respected.
5. Normals: `shade_smooth()` + (Blender 5: smooth-by-angle ≈ 30–40° via the
   `Smooth by Angle` modifier/attribute) so exported `vn` matches a clean shaded look.

## 12. Phase P6 — Export + final acceptance

1. Export with `bpy.ops.wm.obj_export(filepath=..., export_uv=True, export_normals=True,
   export_materials=False, apply_modifiers=True)` to `out/<job>/humanstatue_low_gen.obj`.
   **Do not overwrite `sample/humanstatue_low.obj`.**
2. Post-export validation *outside Blender* with `uv_agent/io/obj_loader.py` (or awk-level
   recount): v/vt/vn present, face count, all faces quads, face indices reference vt/vn.
3. Final acceptance report (`out/<job>/report.json` + `report.md`):
   - all P4 metrics for the generated mesh AND the reference side-by-side;
   - timings + peak memory per phase;
   - silhouette render pairs (generated vs reference, front/side/¾);
   - UV layout PNG (reuse `uv_agent/geometry/preview.py` if it renders layouts).
4. CLI (single command, end-to-end):
   ```
   /Applications/Blender.app/Contents/MacOS/Blender --background --python worker/run_quad_retopo_job.py -- \
     --input sample/humanstatue.obj \
     --reference sample/humanstatue_low.obj \
     --target-faces 2900 \
     --out out/humanstatue_job1
   ```
   The worker orchestrates P1→P6, resumable per phase (each phase writes its artifact;
   `--from-phase P2` reopens `proxy.blend` instead of re-importing 1.86 GB).

## 13. Phase P7 — Tests

- **Blender-free unit tests** (pytest, follow the existing `tests/test_retopo_*.py`
  style): quad gate thresholds, control-loop re-aim math, report schema, OBJ post-export
  validator. Use `retopo_agent/io/fixtures.py` synthetic meshes.
- **Headless Blender integration test** on a SMALL fixture (e.g. subdivided + displaced
  sphere of ~200k faces from fixtures, target 2k quads) running P1→P6 in one Blender
  invocation; assert pure-quad + UV presence + gate pass. Mark it `@pytest.mark.blender`
  / skip when Blender is absent.
- **Acceptance run** (manual, documented): the full humanstatue command from §12.4.
  Paste its report into `docs/QUAD_RETOPO_RESULTS.md`.

## 14. Risks & mitigations

| risk | mitigation |
|---|---|
| Import of 1.86 GB OBJ slow / OOM | new C++ importer; import once, persist proxy.blend (P1.4); 36 GB RAM available |
| Voxel remesh on 24.9M faces too heavy | Collapse to ~2M first, then voxel remesh (P1.3) |
| QuadriFlow unstable at 2.9k target (lower than the 5.8k P0 validated) | seed retries, two-stage ~20k→2.9k, denser proxy (P4 ladder) |
| QuadriFlow needs manifold input | voxel remesh guarantees watertight manifold; assert manifold before P2 |
| Edge flow OK but features mushy | preserve_sharp toggle; relax-then-reproject (P3.2); denser proxy |
| uv_agent planner weak on organic shapes | evaluate with its own metrics; improve planner; Smart UV only as diagnostic baseline |
| Reference is triangle-dominant (not pure quad) — tempts leniency | requirement is stricter than reference: 100% quads, hard gate; use the reference only for shape metrics, not topology |

## 15. Definition of Done

1. `worker/run_quad_retopo_job.py` end-to-end command (§12.4) completes on
   `sample/humanstatue.obj` within the machine's resources.
2. Output: ~2,900 faces (±15%, §1 budget decision), **100% quads**, 0 non-manifold,
   ~3.2k verts or fewer (reference vertex-budget parity), UVs + normals in the OBJ.
3. Shape metrics within 1.5× of the reference low-poly's own metrics (§10).
4. Silhouette renders side-by-side with the reference look equivalent to a human.
5. Unit + integration tests green (`pytest`), Blender test skipped gracefully without Blender.
6. `docs/QUAD_RETOPO_RESULTS.md` records the acceptance run.
