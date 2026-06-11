# Adaptive Low-Poly Agent — Implementation Plan (v2, supersedes quad-flow default)

> Audience: implementation agent. Read fully before coding. This plan **changes the
> default output mode** from uniform pure-quad (QuadriFlow, `docs/QUAD_RETOPO_PLAN.md`)
> to **adaptive decimation** (Decimation-Master-style). The quad plan is NOT deleted —
> it becomes a dormant `--mode quad` to be revived later; do not extend it now.

## 0. Why the pivot (user decision, 2026-06-11)

The user's actual goal, stated after reviewing P2 results:

1. **Silhouette/outline preservation is the top priority.** QuadriFlow at the 2,900
   budget structurally truncated thin extremities (trident tine tips: mesh top at
   z=462.3 vs proxy 526.0; bbox coverage 0.87 on two axes; seed-insensitive across 4
   seeds; full coverage only at ~10k — see `out/quad_p1/p2_exp/p2_coverage.json`).
2. **Polygons should adapt**: large polygons on flat regions, small on detail, and a
   natural mix of triangles and quads — NOT uniform same-size pure quads.
3. The ground-truth reference `sample/humanstatue_low.obj` is itself exactly this kind
   of mesh: 5,799 triangles + 51 quads, 3,191 verts, adaptive sizing.

This is QEM-style adaptive decimation. The earlier decimation effort
(`docs/DECIMATION_MASTER_PLAN.ko.md`) failed NOT because decimation is the wrong
family, but because it fed **raw non-manifold ZBrush soup** (humanstatue source:
52 components, 4,106 non-manifold edges, 519 degenerate faces) straight into
`Decimate(Collapse)`, which plateaued. The validated P1 stage now produces a
**clean watertight manifold 1M proxy** (0 non-manifold, 0 boundary) — the input
Collapse always needed. Mode A = P1 proxy + Collapse on top of it.

**Default mode: `adaptive` (this plan). Future mode: `quad` (frozen plan).**

## 1. Mission

```
input : sample/humanstatue.obj   (24.58M verts / 24.89M faces, no UVs, 1.86 GB)
output: generated low-poly comparable to sample/humanstatue_low.obj
        (adaptive tri/quad mix, silhouette-true, UV-unwrapped)
cli   : --target-faces N  → output lands in N ±10%  (user will request arbitrary
        budgets: 2.9k / 5k / 10k — target tracking is a first-class feature)
```

Reference metrics (ground truth, same world space as the source — direct comparison
is valid; **never overwrite this file**):

| metric | value |
|---|---|
| verts | 3,191 |
| faces | 5,850 (5,799 tris + 51 quads, 0 n-gons) |
| UVs / normals | yes (3,612 vt / 3,523 vn) |

Default `T_goal = 5,850` faces (tri-dominant ⇒ this IS budget parity with the
reference: ~2,900–3,200 verts). The earlier "2,900 quads" decision was quad-mode
arithmetic; in adaptive mode face-count parity and budget parity coincide.

## 2. Hard requirements

1. **Silhouette preservation (top priority).** Thin extremities (trident tines,
   fingers) must survive at the default budget. Hard gates: per-axis bbox coverage
   ≥ 0.98 vs proxy AND directional proxy→low max/p99 distance bounds (§8).
2. **Adaptive polygon distribution.** Tris + quads mixed, sizes varying with local
   detail. 0 n-gons (hard). No uniform-grid look.
3. **Automatic UVs** via `uv_agent` (seams → unwrap → pack); export `vt` + `vn`.
4. **Blender only**, 5.0.1 headless. No external binaries.
5. **Scale**: 24.9M-face input on 36 GB RAM (already proven by P1: 46.9 s, 12.7 GB).
6. **Mode architecture**: `--mode adaptive` (default) | `--mode quad` (existing
   QuadriFlow path, kept compiling + tested but not the default; do not extend).

## 3. What carries over unchanged (validated infrastructure)

From the v1 effort — all proven on the real 24.9M asset:

- **P1 ingest + proxy** (`retopo_agent/blender/proxy.py`, voxel-direct, seeded
  2-probe search → 999,328-face watertight manifold proxy, `proxy.blend` 36 MB,
  source diagnosis, fidelity distribution). Reused as-is.
- **Coverage machinery** from the P2 experiment: directional **proxy→low** sampling
  (mean/p50/p90/p99/max), per-axis bbox coverage, bbox-explosion guard. These become
  the silhouette hard gate (§8) — in adaptive mode they are HARD at the decimation
  stage, not deferred.
- **Worker harness** `worker/run_quad_retopo_job.py`: phase dispatch, per-phase
  artifacts/resume (`--from-phase`), JSON+MD reports. Add `--mode`; rename internals
  only if cheap.
- **uv_agent** (whole package) for P5; `uv_agent/io/obj_loader.py` for post-export
  validation outside Blender.
- Old `retopo_agent` modules that become relevant again for adaptive mode:
  `geometry/importance.py`, `geometry/features.py`, `blender/features.py`
  (feature/importance weighting), `geometry/validate.py`, `blender/shape.py` (BVH
  shape eval), plateau detection concepts from the decimation effort.
- Test suite (196 green) + fixture generators.

## 4. Pipeline (adaptive mode)

```
P1   Ingest + proxy (UNCHANGED)          24.9M → 1M watertight manifold proxy
A2   Adaptive decimate                   Collapse on proxy w/ feature protection,
                                         ratio search → T_goal ±10%, plateau detect
A3   Polygon cleanup                     planar tris→quads (mixed-poly look),
                                         degenerate cleanup, 0-ngon assert
A4   Quality gate + retry ladder         silhouette gates HARD here (not deferred)
P5   Auto UV (uv_agent)                  seams → unwrap → pack, smooth normals
P6   Export + acceptance                 OBJ v/vt/vn, side-by-side vs reference
P7   Tests                               unit + headless integration
```

## 5. Phase A2 — Adaptive decimation on the proxy

Deliverable: `retopo_agent/blender/adaptive_decimate.py`.

1. Open `proxy.blend` (1M faces, manifold). Decimate(Collapse) with
   `ratio = T_goal / proxy_faces` as the seed; **ratio search loop** (reuse
   `target_search.py` pattern) until faces ∈ T_goal ±10%. Expect clean convergence —
   the old plateau came from non-manifold input, which the proxy eliminates. Keep
   plateau detection anyway (alert if 2 consecutive ratio cuts change face count <1%).
2. **Feature protection via vertex weights**: Decimate modifier `vertex_group` +
   `vertex_group_factor`. Build the group from (reuse `blender/features.py` /
   `importance.py`): high curvature, thin-feature regions (small local bbox /
   tube-like radius), and optionally silhouette-critical areas. Start WITHOUT
   protection (QEM alone preserves silhouettes well on manifold input — measure
   first), enable as a retry rung if extremities thin out.
3. `use_collapse_triangulate=True` (avoids degenerate quads during collapse).
4. Light **shrinkwrap (NEAREST) back to proxy** after apply — collapse places verts
   at QEM-optimal positions slightly off-surface; one nearest-point snap restores
   contact. Measure shape before/after; keep only if it improves.
5. Record per-attempt: faces, tris/quads/ngons, non-manifold, components, bbox
   coverage, proxy→low distances, wall time.

## 6. Phase A3 — Polygon cleanup (mixed tri/quad look)

Deliverable: part of `adaptive_decimate.py` or sibling module.

1. **Tris→Quads** (`bpy.ops.mesh.tris_convert_to_quads`) with conservative angle
   limits (face_threshold/shape_threshold ≈ 10–25°) — merges coplanar triangle pairs
   into quads exactly where quads "fit", leaving triangles elsewhere. This produces
   the requested natural tri/quad mix (the reference's 51 quads are this pattern).
   Tune so quad share stays organic; do NOT chase a quad-ratio number.
2. Cleanup: dissolve degenerate edges (`mesh.dissolve_degenerate`), then asserts:
   **0 n-gons (hard)**, 0 non-manifold (hard), components ≤ proxy's meaningful
   components (1 — the proxy's comp1 is a 12-vert floater; drop it in P1 or here,
   log it), face count still within band after cleanup (re-aim A2 if cleanup
   moved it out).
3. Normals: `shade_smooth` + smooth-by-angle (~30–40°).

## 7. Phase A4 — Quality gate + retry ladder

Deliverable: `retopo_agent/geometry/adaptive_gate.py`.

Baseline first: evaluate the REFERENCE vs proxy (same world space) and record its
mean/p95 surface distance + normal deviation. Generated mesh passes if:

| check | threshold | kind |
|---|---|---|
| n-gons | 0 | hard |
| non-manifold edges | 0 | hard |
| per-axis bbox coverage vs proxy | ≥ 0.98 each axis | hard, NOT calibratable |
| proxy→low max distance | ≤ reference's max × 1.25 | hard |
| proxy→low p99 | ≤ reference's p99 × 1.25 | hard |
| face count | T_goal ±10% | hard |
| vertex count (at default T_goal) | ≤ 3,191 × 1.15 | sanity |
| low→proxy mean distance / normal dev | ≤ 1.5 × reference's | soft → retry |
| fixed-camera silhouette renders | human review | report |

**Render rule (mandatory):** all comparison renders use ONE fixed camera shared by
proxy/reference/generated — auto-framed per-mesh renders are forbidden as evidence
(they hid the tine truncation in v1).

Retry ladder (cheap → expensive, log every rung):
1. ratio re-aim (band miss)
2. tris→quads thresholds tweak (A3 broke a gate)
3. enable/strengthen feature-protection vertex group (extremity thinning)
4. shrinkwrap snap on/off (shape metrics)
5. denser proxy (1.5M; smaller voxel) — thin features under-resolved at 1M
6. report `failed` with best attempt + full history. Never silently ship a
   gate-violating mesh.

## 8. Phases P5–P7 — UV, export, tests (same as v1 plan §11–§13, with deltas)

- P5 (uv_agent): unchanged contract; note the mesh is now tri/quad mixed — verify the
  seam planner and unwrap handle triangles (they should; reference-style meshes are
  the norm). UV acceptance via `uv_agent/geometry/evaluation.py` (no overlaps,
  stretch band, margins).
- P6 export/acceptance: unchanged + fixed-camera render rule + side-by-side metrics
  table generated-vs-reference. CLI:
  ```
  Blender --background --python worker/run_quad_retopo_job.py -- \
    --input sample/humanstatue.obj --reference sample/humanstatue_low.obj \
    --mode adaptive --target-faces 5850 --out out/humanstatue_adaptive1
  ```
  Also run `--target-faces 10000` and `2900` once each — arbitrary-budget tracking
  is a first-class user requirement; record all three in results.
- P7 tests: keep 196 green; add unit tests for gate thresholds/re-aim math/cleanup
  asserts + a small-fixture headless integration test for `--mode adaptive`
  (`@pytest.mark.blender`). Acceptance run results → `docs/ADAPTIVE_LOWPOLY_RESULTS.md`.

## 9. Risks

| risk | mitigation |
|---|---|
| Collapse plateaus even on manifold proxy | plateau detector + denser-proxy rung; evidence says unlikely (plateau was non-manifold-driven) |
| Extremities thin out (not truncate) at low budgets | feature-protection vertex group rung; bbox+max-distance hard gates catch it |
| tris→quads pushes count out of band | re-aim loop closes over A2+A3 combined |
| UV planner struggles on mixed tri/quad organic mesh | uv_agent metrics gate it; planner fixes over Smart-UV fallback |
| Mode split rots quad path | quad mode stays tested-but-frozen; no new features |

## 10. Definition of Done

1. End-to-end `--mode adaptive` completes on humanstatue at T_goal 5,850 — and also
   at 10,000 and 2,900 (budget-tracking demo).
2. Default-budget output: 5,850 ±10% faces, tri/quad mix, 0 n-gons, 0 non-manifold,
   bbox coverage ≥ 0.98/axis (tines intact), UVs + normals exported.
3. Hard gates green; shape metrics within bands vs reference baseline.
4. Fixed-camera silhouette renders side-by-side with the reference.
5. Tests green (incl. new adaptive suite); quad mode still green/frozen.
6. `docs/ADAPTIVE_LOWPOLY_RESULTS.md` records the three acceptance runs.
