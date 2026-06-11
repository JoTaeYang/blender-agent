# UV Planner Repair Plan — organic low-poly meshes

> Audience: implementation agent. Scope: make `uv_agent` produce shippable UVs for
> the adaptive low-poly outputs (tri-dominant organic meshes, ~3k–10k faces) so the
> Smart-UV-Project fallback is no longer the shipped path. Read
> `docs/ADAPTIVE_LOWPOLY_PLAN.md` §8 for pipeline context.

## 1. Failure evidence (acceptance runs, 2026-06-11)

All three budgets shipped the fallback. Planner self-evaluation on the 5,850 mesh
(`out/adaptive_acc_5850/p5_evaluation.json`):

| metric | planner result | shippable needs |
|---|---|---|
| island_count | **551** | ~5–30 |
| small_island_ratio | **0.989** | ≤ 0.2 |
| overlap_ratio | 0.2204 | 0 |
| stretch_score | 0.6934 | band (≤ ~0.35, calibrate §5) |
| packing_efficiency | 0.348 | ≥ 0.6 |
| status | needs_repair | accepted |

Reference artist UVs for comparison: `sample/humanstatue_low.obj` has vt/v = 1.13
(3,612 vt / 3,191 v) — i.e. *few long seams, large islands*. Our shipped fallback has
vt/v ≈ 2.5 (confetti islands from Smart Project).

## 2. Root cause

`uv_agent/planner/island_planner.py::is_seam_edge` (line ~105) cuts at every edge
with `dihedral_angle >= angle_threshold (30°)`. On a faceted low-poly organic mesh,
a large fraction of ALL edges exceed 30° dihedral — the flood fill shatters the mesh
into 551 fragments. The strategy (dihedral = seam) is a hard-surface assumption; it
is categorically wrong for organic meshes, where seams must be **few, long,
deliberately-placed paths** (inner arm, robe side, under-staff), not local angle
events. Per-island `planar` projection of 3D-curved fragments then yields the
overlap and stretch numbers above.

No amount of threshold tuning fixes this (raise to 60° → islands merge but each
island is highly curved → planar projection stretch explodes). The projection and
seam strategy must change together.

## 3. Strategy

Two tracks, in order. Track 1 is the deliverable; Track 2 is its quality loop.

### Track 1 — organic seam generation + proper unwrap (replaces dihedral shatter)

New planner mode `seam_strategy="organic"` (keep `"hard_surface"` as the existing
behavior for CAD-ish inputs; pick by a cheap mesh statistic, e.g. fraction of edges
over the dihedral threshold > ~25% ⇒ organic).

1. **Disk-topology base cuts.** Each UV island must be a topological disk. Compute
   the minimal cut set: for a closed genus-0 component a single short seam path is
   enough to open it; add one cut per handle (genus) if present. Implementation:
   shortest-path seams (Dijkstra over edges, weighted to prefer concave valleys and
   high mean-curvature edges — reuse curvature machinery from
   `retopo_agent/geometry/features.py`).
2. **Extremity cuts.** Tube-like protrusions (trident shaft/tines, arms, legs) need
   a seam along their length plus a loop cut at the junction, or the unwrap
   collapses them. Detect via the existing thin-feature/importance tools
   (`retopo_agent/geometry/importance.py`) or a simple geodesic-distance extremity
   detector (farthest-point sampling: extremity tips = local maxima of geodesic
   distance from mesh centroid).
3. **Unwrap with a real parameterizer, not planar projection.** The mesh lives in
   Blender at P5 time — use `bpy.ops.uv.unwrap(method='ANGLE_BASED')` (ABF) or
   `'MINIMIZE_STRETCH'` honoring the seams marked by steps 1–2 (`edge.use_seam`).
   Drop per-island `projection="planar"` for organic mode entirely; keep
   `uv_agent/geometry/relaxation.py` as a post-relax if it measurably helps.
4. **Pack** with `bpy.ops.uv.pack_islands(margin=...)` (Blender's packer is strong
   in 4.x/5.x; rotation on) or the existing `packing.py` if it scores better.

### Track 2 — stretch-driven seam refinement loop (the agent loop)

After Track 1 unwrap, iterate (max ~5 rounds):

1. Evaluate per-face stretch (extend `uv_agent/geometry/evaluation.py` to return a
   per-face stretch map, not just the global score).
2. If global stretch in band → stop.
3. Else: in the worst island, grow ONE new seam along the steepest-distortion path
   (shortest path through highest-stretch faces from the island's stretch peak to
   the island boundary), re-unwrap that island, re-pack.
4. Each round must monotonically reduce stretch; if a round doesn't, revert it and
   stop (report best state).

This mirrors classic stretch-refinement parameterization and maps cleanly onto the
existing planner-actions architecture (`split_island` actions with concrete edge
paths) — the LLM/mock agent can stay in the strategy seat.

## 4. What to keep / change

- KEEP: planner-action architecture (`operations.py`), `IslandPlan` data model,
  evaluation pipeline + JSON reports, Smart-UV as a *diagnostic baseline only*
  (compute its metrics for the report; never ship it when organic mode is on).
- CHANGE: `island_planner.py` gains the organic strategy (§3); `pipeline.py` P5
  applies seams in Blender and calls real unwrap; `evaluation.py` gains per-face
  stretch + vt/v ratio + island-size histogram.
- REMOVE (organic mode): planar/cylindrical per-island projection as the unwrap.

## 5. Gates (P5 acceptance, hard unless noted)

Calibrate the stretch band first: run the evaluator on the REFERENCE's own UVs
(`sample/humanstatue_low.obj` has vt — load and score it). Then:

| check | threshold |
|---|---|
| overlap_ratio | 0 (hard) |
| island_count | ≤ 30 (param; reference-style is ~5–15) |
| small_island_ratio | ≤ 0.2 |
| vt/v ratio | ≤ 1.5 (reference: 1.13) |
| stretch_score | ≤ reference's score × 1.25 (hard once calibrated) |
| packing_efficiency | ≥ 0.6 |
| UDIM/0-1 bounds | all UVs in [0,1] |
| fallback used | **false** (hard — Smart-UV may appear in the report as baseline only) |

Failure after Track 2 exhausts → report `failed` with best attempt + metric history
(same no-silent-shipping rule as A4).

## 6. Tests & acceptance

- Unit (Blender-free): disk-cut computation on fixture meshes (closed sphere needs
  1 cut; torus needs handle cut), extremity detector on a capsule-with-spikes
  fixture, stretch-map math, gate thresholds.
- Headless integration: small organic fixture (displaced sphere ~2k faces, one
  protrusion) through P5 organic mode — gates pass, no fallback.
- Acceptance: re-run P5+P6 on the three existing A4-accepted meshes
  (`out/adaptive_acc_{2900,5850,10000}/adaptive_t*.blend` — resume from those, do
  NOT redo P1–A4). Deliver per-budget: UV layout PNG, metrics vs reference
  side-by-side, updated OBJ. Record in `docs/ADAPTIVE_LOWPOLY_RESULTS.md`.

## 7. Risks

| risk | mitigation |
|---|---|
| Auto extremity cuts misplace seams (visible on front) | seam_visibility weighting in Dijkstra cost (penalize front-facing edges); report renders seams over the mesh |
| Stretch loop fragments islands again | hard island_count + small_island_ratio gates; one-seam-per-round + monotonic-improvement rule |
| Blender pack_islands margin vs evaluator disagreement | single source of truth: evaluator runs on the final packed UVs |
| Genus detection wrong on messy shells | components are watertight post-P1 (Euler characteristic is reliable); assert and fall back to one-cut-then-measure |

## 8. Definition of Done

1. P5 organic mode ships planner UVs (fallback=false) on all three acceptance
   meshes with §5 gates green.
2. vt/v ≤ 1.5 and island_count ≤ 30 on the 5,850 mesh (reference-style layout).
3. UV layout PNGs + seam-overlay renders in each acceptance folder.
4. Full test suite green (existing 225 + new UV tests).
