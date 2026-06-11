# Adaptive Low-Poly — Acceptance Results (plan §10 DoD)

End-to-end `--mode adaptive` runs on the real `sample/humanstatue.obj` (24.9M faces)
via the validated P1 proxy (999,328-face watertight manifold). Blender 5.0.1 headless,
one process per budget. Reference = `sample/humanstatue_low.obj` (5,850 faces, 3,191
verts), measured against the same proxy in the same world space.

Command (per budget — **one process = one target**):

```
Blender --background --python worker/run_quad_retopo_job.py -- \
  --mode adaptive --reference sample/humanstatue_low.obj \
  --target-faces <N> --out out/adaptive_acc_<N>
# (--from-phase A2 reuses an existing proxy.blend; default runs P1 first)
```

## Three-budget acceptance (all gate PASS)

| budget | faces | tris / quads / n-gons | verts | non-manifold | components | bbox min-axis | verdict | wall | peak RSS |
|---|---|---|---|---|---|---|---|---|---|
| **2,900** | 2,851 | 2,802 / 49 / **0** | 1,446 | 0 | **1** | **0.9944** | ✅ PASS | 81 s | 2.93 GB |
| **5,850** | 5,756 | 5,662 / 94 / **0** | 2,921 | 0 | **1** | **0.9992** | ✅ PASS | 130 s | 2.7 GB |
| **10,000** | 9,821 | 9,642 / 179 / **0** | 4,996 | 0 | **1** | **0.9984** | ✅ PASS | 155 s | 2.8 GB |

All within T_goal ±10% (errors 1.6–1.8%). Adaptive tri/quad mix (49–179 quads,
budget-proportional, organic — reference has 51). Hard silhouette gate green on every
axis (≥0.98). proxy→low max/p99 distances are far under the reference baseline
(`proxy_to_ref_max≈14.6`, `p99≈1.43`) — the generated meshes cover the proxy better
than the ground-truth reference does.

## What each phase produced

- **P1 floater drop**: the 12-vert / 10-face stray shell is removed, so the proxy is a
  single watertight body and A3/A4 assert the tight `components == 1` (was 2).
- **A2 adaptive decimate**: Collapse + ratio search converged in-band on the first
  attempt at every budget (no plateau, no retry rung needed). Shrinkwrap snap kept
  only where it improved mean distance (10k/2.9k kept it, 5,850 discarded it).
- **A3 tris→quads cleanup**: conservative 15° merge → natural tri/quad mix, 0 n-gons,
  shade-smooth. Hard asserts green.
- **A4 gate**: all hard + soft checks pass; `next_rung` = none. Retry ladder unused
  (covered by 16 unit tests).
- **P5 auto-UV**: uv_agent planner flagged `needs_repair` (≈22% overlap on the organic
  mesh, as the plan's risk row predicted) → **Smart-UV Project fallback** applied,
  non-overlapping by construction. UVs exported as `vt`.
- **P6 export**: `adaptive_t<N>.obj` (v/vt/vn) + `.blend` + fixed-shared-camera
  front/side renders of **both** generated and reference (auto-framed renders are
  forbidden as evidence — plan §7).

Exported OBJ vertex/uv/normal/face line counts confirm v+vt+vn present, e.g. 5,850:
`v 2921 / vt 7303 / vn 5035 / f 5756`.

## Silhouette evidence

Fixed-camera front renders (`out/adaptive_acc_<N>/adaptive_t<N>_generated_front.png`
vs `..._reference_front.png`). Trident tines survive at **all** budgets including
2,900 — the v1 QuadriFlow failure (tine truncation, bbox 0.87) is resolved.

## Known limitations / follow-ups

- **UV quality**: the uv_agent planner does not yet pass its own metrics gate on this
  organic tri/quad mesh; we ship the Smart-UV fallback. Improving the planner (or
  tuning the fallback's island margin / packing) is follow-up, tracked against the
  plan §9 risk row.
- **Denser-proxy rung** (ladder rung 5) is logged as a recommendation, not
  auto-executed — it needs a P1 re-run with a smaller voxel. Rungs 1–4 are wired.
- All three accepted on the first attempt, so the retry ladder was not exercised on
  the real asset (it is unit-tested).
