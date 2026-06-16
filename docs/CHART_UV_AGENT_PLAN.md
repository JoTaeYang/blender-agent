# Chart-Based UV Agent — Implementation Plan (uv_agent v2)

> **2026-06-13 STATUS — ALSO THE LOWER-LEVEL FALLBACK / COMPONENT for the artist
> engine.** `docs/AUTO_ARTIST_UV_PLAN.md` adds `--uv-engine artist`, a semantic
> *artist-style* path. It reuses this engine's primitives directly: `split_chart`
> / `flood_charts` / `normal_cone_halfangle` drive the artist seam layer's diskify
> + cone-split, and a `blob`/`unknown` part falls back to chart-style segmentation
> *for that part only*. `chart` stays the default generic engine; `artist` is the
> no-reference artist-style target (NOT default yet — needs the §AR7 fixture suite).
>
> **2026-06-13 STATUS — GENERIC DEFAULT ENGINE.** Per
> `docs/GENERIC_UV_REVISION_PLAN.md`, this is the **default P5 engine** for the
> general low-poly→UV product path: `--uv-engine auto` resolves to `chart`
> UNCONDITIONALLY (even when a reference happens to carry UVs). It is the right
> default because it needs no reference and makes no part-based / slot
> assumptions. Reference-guided transfer (`docs/UV_TRANSFER_PLAN.md`) is now an
> *explicit, reference-assisted mode only* (`--uv-engine transfer`), NOT the
> default — a single reference's chart topology must not leak onto arbitrary
> assets. Future quality work concentrates here, in chart segmentation/repair and
> generic multi-asset fixtures, not in matching one artist layout.
>
> The gate (`chart_uv_agent/gate.py`) measures UV *usability* — correctness +
> generic quality — not resemblance to any reference. Its numeric thresholds are
> calibrated acceptance defaults pinned on one asset and MUST be recalibrated on a
> multi-asset fixture set before production (GENERIC_UV_REVISION_PLAN §G3).

> **Superseded note (kept for history):** geometric targets reached (stretch
> 0.2–0.3, raster overlap fixed via §5d), but an earlier team-lead review rejected
> the layout as not *semantically* similar to one reference. That drove the
> reference-transfer experiment; the generic revision (above) re-establishes this
> engine as the product default.

> Audience: implementation agent. This plan creates a NEW UV engine that replaces
> `uv_agent` as the P5 stage of the adaptive low-poly pipeline
> (`docs/ADAPTIVE_LOWPOLY_PLAN.md`). The existing `uv_agent` package stays in the
> repo untouched (baseline + its own demo path); the pipeline swaps engines.

## 1. Why a new engine (user decision, 2026-06-11)

The repaired organic-pelt unwrap (`docs/UV_REPAIR_PLAN.md`) passed its hard gates
(overlap ~0, 1–6 islands, vt/v 1.07–1.22, no fallback) but the user compared the
layouts and rejected the *style*:

- **Desired** (attachment `UeBcMk`, artist-style): many well-formed charts cut by
  body part — torso panels, robe strips, limbs, props — near-straight boundaries,
  near-uniform texel density, tightly packed (~75%+ of UV space).
- **Got** (attachment `fjzjNX`, organic pelt): 1–6 huge petal-shaped islands,
  wavy boundaries, stretch 1.5–3.2, packing ~0.49.

The seam-density sweep (`out/uv_sweep.log`) already proved the pelt family cannot
reach low stretch at any island count (53 islands → stretch still 1.10; the only
low-stretch result, Smart-UV 0.116, got there via *near-planar charts*). Conclusion:
**low distortion requires near-developable chart decomposition**, not more seams on
a pelt. That is what this engine does — and it is structurally how the reference
artist UV works (51 pre-separated shells acting as charts).

## 2. Goal & target metrics

Input: A4-accepted adaptive meshes (tri-dominant, watertight, 2.9k–10k faces, in
Blender at P5 time). Output: artist-style UV layout.

| metric | target | note |
|---|---|---|
| overlap_ratio | ≤ 0.001 (hard) | |
| stretch_score (area) | **≤ 0.5 (hard)** | the metric the pelt could never hit; calibrate exact bar in U0 |
| packing_efficiency | **≥ 0.50 (hard)** | recalibrated 2026-06-12: the 0.76 reference number is *manual* nesting; auto-repacking the reference's own charts with Blender CONCAVE yields 0.626 (independently reproduced) — auto-packer ceiling. See ADAPTIVE_LOWPOLY_RESULTS.md |
| island_count | **as few as possible**, hard cap 60 | user directive: minimize islands; only split when distortion demands it (§5) |
| small_island_ratio | ≤ 0.25 | confetti guard stays |
| texel_density_variance | ≤ calibrated band | uniform checker size across charts |
| chart boundary quality | report-only | straightness/jagged score, no hard bar yet |
| vt/v ratio | **≤ 2.0 (relaxed)** | more charts ⇒ more split verts; reference 1.13 is a shell-count artifact, not reachable with welded input |
| fallback_used | false (hard) | Smart-UV remains diagnostic-only |
| uv bounds [0,1] | hard | |

Note the gate philosophy changed with the user's priorities: stretch and packing
move from SOFT to HARD; island_count and vt/v loosen. Keep every value in one
config dataclass — U0 calibrates them.

## 3. Architecture

New package `chart_uv_agent/` (sibling of `uv_agent`, reuse via imports — do not
fork code):

```
chart_uv_agent/
  segmentation.py      # U1: mesh → charts (the core novelty)
  unwrap.py            # U2: per-chart Blender unwrap + straighten/relax
  packing.py           # U3: pack + density normalize (Blender pack_islands first)
  gate.py              # U4: gates above (reuse uv_agent/geometry/evaluation.py metrics)
  pipeline.py          # U1→U4 orchestration + refinement loop + report
tests/test_chart_uv_*.py
```

Pipeline integration: `worker/run_quad_retopo_job.py` P5 calls `chart_uv_agent`
behind `--uv-engine chart` (new default). `--uv-engine organic` keeps the v1 path
for comparison. Old `uv_agent` planner untouched.

## 4. Phase U0 — Calibration & fixtures (half a day, do first)

1. Run the metric suite on three layouts of the SAME 5,850 mesh: (a) organic-pelt
   result (`out/uv_acc_5850`), (b) Smart-UV diagnostic, (c) reference
   `humanstatue_low.obj` UVs. Pin the exact hard thresholds of §2 from this table
   (e.g. stretch bar = max(0.5, smart_uv × 1.5)) and record them in the plan/report.
2. Extend `uv_agent/geometry/evaluation.py` (in place, additive) with:
   texel_density_variance per chart, boundary-straightness score.
3. Fixtures: capsule-with-spikes + displaced sphere + a humanoid-ish blob for unit
   tests (reuse `retopo_agent/io/fixtures.py` generators where possible).

## 5. Phase U1 — Chart segmentation (the core)

Goal: decompose the mesh into 15–60 near-developable, compact charts whose
boundaries land on natural lines (the desired image's look).

**Two user directives (2026-06-11) define the control law — encode them verbatim:**

> (R1) Minimize the number of UV islands; whenever distortion exceeds the threshold,
> increase the island count (split) — and only then.
> (R2) Any edge where the model bends ≥ 90° (dihedral) is ALWAYS cut as a UV seam —
> unconditional, not a heuristic weight.

Algorithm — **mandatory-seam partition + distortion-driven splitting** (fewest
islands satisfying the stretch bar), pure Python/numpy on the MeshGraph:

1. **Mandatory seams (R2)**: mark every edge with dihedral ≥ 90° as a forced seam
   (plus mesh boundary/non-manifold edges, which are seams by definition). On the
   adaptive low-poly meshes this is a small, meaningful set (only real creases —
   for context, the 30° threshold that caused the 551-island confetti covered ~30%
   of edges; 90° covers only genuine folds).
2. **Initial charts = flood fill** across faces without crossing forced seams →
   the minimal island set consistent with R2. Make each chart a topological disk:
   closed charts get one shortest-path cut; handles get handle cuts. Tube-like
   charts (trident shaft/tines, arms — reuse the extremity detector) get a single
   lengthwise seam (cylinder strip, the long thin strips in the desired layout).
3. **Unwrap + measure** (U2), then the R1 loop: while any chart's stretch exceeds
   the bar, split ONLY the worst-offending chart and re-unwrap it; repeat. Split
   method: VSA-style 2-way normal clustering inside the chart (two farthest seeds,
   region-grow by normal-deviation + compactness cost, boundary discount on
   concave/high-dihedral edges so the new seam lands in a fold), then boundary
   smoothing (step 5). Stop when all charts meet the bar or the 60-island cap hits
   (cap hit ⇒ report `failed`-with-best, never silently ship).
4. **Merge pass (R1 minimality)**: after convergence, greedily try merging adjacent
   chart pairs whose union would still satisfy the stretch bar AND whose shared
   boundary is not a forced (R2) seam; accept merges that keep all gates green.
   This squeezes the island count back down after over-eager splits.
5. **Boundary smoothing**: shortest-path re-route of non-forced chart boundaries
   (Dijkstra on the boundary band, edge cost favoring straightness + concavity) to
   kill jagged staircase borders. Forced (R2) seams are never re-routed away —
   they may only be straightened along equally-qualifying ≥ 90° edges.

Output: face→chart map + seam edge set. Unit-testable without Blender.

## 5b. Phase U1.6 — Chart shape repair (geodesic boundary re-routing)

> Added 2026-06-13 after user side-by-side review of the t5850 layout vs the
> reference UV. Verdict: chart *composition* matches (part-based charts, uniform
> density) but chart *shape* does not — our boundaries are the raw trace of
> region-growing: jagged, with thin tendrils, deep concavities, and sliver
> fragments, which also causes the packing holes. The artist's charts have smooth,
> compact, convex-ish outlines. U1.5 (local face re-labeling) reduced jaggedness
> but cannot fix macro shape. This phase replaces boundary *adjustment* with
> boundary *re-routing*. It runs after segmentation converges, before U2 unwrap.

Three operations, applied in order, iterated to a fixed point (max ~5 rounds):

1. **Geodesic boundary replacement.** For each non-forced boundary between two
   charts: identify its endpoints (junction vertices where ≥3 charts meet, or the
   boundary loop's extremal pair for a 2-chart boundary). Replace the whole
   boundary polyline with the weighted-shortest path between those endpoints
   (Dijkstra on mesh edges restricted to a band around the old boundary, cost =
   edge length × (1 + concavity bonus) — prefer short, smooth, valley-following
   paths). Re-assign faces between old and new path by flood fill. Constraints:
   forced (R2) ≥90° seams are NEVER replaced or crossed; both charts must remain
   connected disks; reject a replacement that increases that pair's combined
   unwrap stretch beyond the bar.
2. **Tendril amputation.** Detect chart sub-regions of width ≤ 2–3 faces
   (faces whose distance-to-boundary is ≤1 on both sides, forming a chain):
   cut the tendril at its base and absorb it into the neighboring chart it
   borders most. Slivers/fragments below the small-chart area threshold are
   absorbed entirely (existing absorb pass, but re-run after every re-route).
3. **Concavity split.** Compute each chart's 2D outline (after a cheap unwrap or
   via boundary turning angles in 3D): if a chart has a concavity deeper than a
   threshold (e.g. pocket depth > 25% of chart diameter), split it at the two
   deepest concave vertices into two compact charts (this is the artist behavior:
   convex-ish pieces). Counted against the 60-island cap; R1 merge pass re-runs
   afterwards.

**Shape gates (new, evaluated per chart after U1.6):**

| metric | bar | rationale |
|---|---|---|
| boundary smoothness: boundary_edge_count / geodesic_endpoint_distance per segment | ≤ calibrated from reference charts (U0-style: measure the reference's own charts first) | kills staircase/tendril traces |
| chart convexity: chart UV area / convex hull area | ≥ calibrated from reference (expect ~0.7–0.8) | kills deep pockets, the direct cause of packing holes |
| tendril count (width ≤2 chains longer than 4 faces) | 0 (hard) | |

Calibrate both bars by measuring the REFERENCE's 39 charts with the same code
before setting numbers — do not invent thresholds.

Expected side effect: packing should rise above the current 0.605 blob-shape
ceiling (the holes are concavity-driven); re-measure and report, but the packing
bar stays 0.50 — any gain is recorded, not gated.

Acceptance for this round: re-run P5+P6 on the same three A4 meshes; all §2 hard
gates stay green; the NEW shape gates pass; layout PNGs re-reviewed by the user
side-by-side against the reference (the real bar). Tests: unit tests for each of
the three operations on fixtures (a chart with an artificial tendril / concavity /
jagged boundary), determinism, R2-seam preservation property test; full suite
stays green.

> STATUS 2026-06-13: implemented, 3 budgets accepted (convexity_mean 0.73–0.84,
> verified). User review: clearly improved but the WORST charts still read as
> ragged — convexity_p10 = 0.44. One final shape round follows (§5c).

## 5c. Phase U1.7 — Tail round (worst-chart convexity), FINAL shape round

User decision 2026-06-13. The mean convexity gate passes but the eye catches the
tail: the bottom-decile charts (spiky protrusions, notches) are what still
separates our layout from the reference. One more round, targeting the tail only:

1. **Calibrate first (same principle — no invented numbers).** Measure the
   reference's 39 charts with the same convexity code and record their p10 /
   per-chart minimum. Set the new bar from that (expected region ~0.55–0.60;
   use what the measurement says).
2. **New gate: per-chart convexity tail** — `convexity_p10 ≥ <calibrated>`
   (hard). The existing mean gate stays.
3. **Driver change:** the concavity-cut loop (§5b op 3) iterates not to mean
   convergence but **until every below-bar chart is fixed or provably stuck**:
   for each chart below the bar, try (a) concavity cut, (b) absorbing its
   protruding sub-region into a neighbor (inverse of tendril amputation —
   donate the spike to the chart it points into, if the donor stays a disk and
   the receiver's convexity does not drop below the bar), (c) merge with a
   neighbor if the union is more convex AND stretch stays in band. A chart is
   "provably stuck" when all three moves are rejected by the invariants
   (disk/≥5-face/R2/stretch) — report it explicitly with the reason.
4. **Budget:** island cap 60 unchanged (currently 36–44, there is room). All
   existing gates (stretch ≤0.5, overlap, packing ≥0.50, fallback=false, R2
   preservation) stay hard.
5. **Scope discipline: this is the LAST shape round.** Whatever it yields,
   ship and stop — the result goes to user review and then commit, no further
   shape work without a new user decision (diminishing-returns line drawn here).

Acceptance: same as §5b (3 budgets re-run, all gates green incl. the new tail
gate or explicit stuck-chart report, PNGs to the user side-by-side). Tests:
fixture with one deliberately spiky chart → tail loop fixes it; stuck-chart
reporting path covered; full suite green.

## 5d. Phase U2.5 — True-overlap correctness round (raster gate + SLIM repair)

> Added 2026-06-13 after the user found real UV overlaps the gate missed. Root
> cause: `overlap_ratio` only measures flipped-triangle area; concave/curved
> charts self-fold under ABF *without* flipping. Independent raster measurement:
> shipped meshes 2.8–5.7% multi-covered UV pixels, reference 0.0000.

**Detection (implemented, keep):** `raster_overlap_ratio` — UV faces rasterized
to a ≥1024² pixel-center grid, multi-occupied/occupied, 1px erosion to drop
shared-edge aliasing. HARD gate ≤ 0.005 (reference measures 0.0000 with the same
code). Attribution: self-intersection vs cross-island, reported per budget.
Keep the flipped-area metric too (different defect classes).

**Repair path — SLIM first, split last.** The first repair attempt
(CONFORMAL re-unwrap + fold-splitting) removed the overlap but exploded charts
to 63–105 (island cap broken) and pushed stretch to 0.52 — because LSCM has NO
injectivity guarantee, so folds were being fixed by subdivision alone. Blender
4.3+/5.x ships SLIM (`uv.unwrap(method='MINIMUM_STRETCH')`), which is
**guaranteed locally injective (flip/fold-free)** — it removes self-folds
without splitting:

1. Detect self-overlapping charts via the raster metric.
2. Re-unwrap ONLY those charts with SLIM (`MINIMUM_STRETCH`); re-measure.
3. Charts still overlapping after SLIM (rare: boundary self-overlap on extreme
   shapes) → split that chart, SLIM again. Splitting is the exception path,
   not the driver.
4. Cross-island overlap (none observed; packer doesn't overlap) → margin bump
   + repack, as before.
5. Report SLIM-before/after table per budget: raster_overlap, chart count,
   stretch. Expected outcome: raster ≤0.005 at ~40–60 charts with stretch at
   or below the ABF numbers — i.e. the island_count↔overlap trilemma dissolves.
   If SLIM cannot reach the bar within the 60-cap, STOP and report (user then
   re-decides between cap relaxation and upstream topology work — do not pick
   unilaterally). Accepting residual overlap (recalibrating the raster bar up)
   is NOT an option: overlapping UVs corrupt texture baking by construction.

Acceptance: 3 budgets re-run; ALL hard gates green including raster ≤0.005 AND
island_count ≤60; no fallback; independent raster re-measure must agree. Tests:
SLIM repair path on a self-folding fixture chart; the existing raster tests stay.

## 6. Phase U2 — Per-chart unwrap

1. Mark chart boundaries as seams in Blender; `uv.unwrap(method='ANGLE_BASED',
   margin=0)` once for the whole mesh (charts unwrap independently given seams);
   `uv.minimize_stretch` few iterations per worst charts.
2. Per-chart validation: flipped/zero-area triangles → re-unwrap that chart with
   `'MINIMIZE_STRETCH'`; still bad → split the chart (U1.4) and redo.
3. **Density normalize**: `uv.average_islands_scale` so every chart has the same
   texel density (the desired image's uniform wireframe density).

## 7. Phase U3 — Packing

1. Primary: Blender 5 `uv.pack_islands(rotate=True, margin=4–8px@1024,
   shape_method='CONCAVE')` — modern Blender packing is strong; measure first.
2. If packing < bar: try rotate_method variants and margin reduction before custom
   code; only fall back to `uv_agent/geometry/packing.py` improvements if Blender's
   packer genuinely can't reach 0.70 (record evidence).

## 8. Phase U4 — Gate + refinement loop

Gate of §2. The R1 split/merge loop (§5.3–5.4) is the primary mechanism and runs
inside U1–U2. This phase handles the remaining knobs (max ~6 rounds,
monotonic-best like A4):

- stretch fail that the R1 loop could not close → already `failed`-with-best (§5.3)
- packing fail → margin/rotation retune; if still failing, split the largest chart
  (big blobs pack badly) — allowed because R1 permits splits when a gate demands it
- too many islands after refinement → §5.4 merge pass re-run
- texel variance fail → re-run average_islands_scale; investigate protected charts

Report: layout PNG per round, metric history, final side-by-side vs organic-pelt
and reference. Same no-silent-shipping rule: hard-gate failure after the loop →
`failed` + best attempt kept.

## 9. Acceptance (P5+P6 resume — do NOT redo P1–A4)

Resume the three A4-accepted meshes (`out/adaptive_acc_*/adaptive_t*.blend`):
2,900 / 5,850 / 10,000 with `--uv-engine chart`. Deliverables per budget in ONE
final folder per budget (this also resolves the stale-folder confusion between
`adaptive_acc_*` and `uv_acc_*` — consolidate; RESULTS doc points only at the
final folders):

- updated OBJ (v/vt/vn) + .blend
- UV layout PNG (must look like the desired attachment style: many straight-edged
  charts, tight packing) + checker-texture render (front, fixed camera)
- gate JSON + metric comparison table (chart vs organic vs Smart-UV vs reference)

User reviews the layout PNGs — the desired-image style is the acceptance bar that
matters most; metrics exist to automate it.

## 10. Tests

- Unit (Blender-free): segmentation determinism + chart connectivity/disk
  invariants on fixtures; tube detection; merge/split logic; boundary smoothing
  reduces jaggedness score; gate thresholds.
- Headless integration: displaced-sphere + capsule fixture through U1→U4, hard
  gates pass, no fallback.
- Keep the full existing suite green (240); organic engine tests stay.

## 11. Risks

| risk | mitigation |
|---|---|
| Region growing produces ragged boundaries | U1.6 (§5b) geodesic boundary re-routing is mandatory, not optional; shape gates calibrated from reference charts |
| U1.6 re-routes degrade stretch (U1.5 already cost 0.15→0.26) | per-pair stretch re-check rejects any re-route that pushes past the bar; report stretch before/after |
| Charts won't reach stretch ≤ 0.5 on heavy-curvature areas (head/hands) | cone-limit split loop (U8); these become several small charts — that's what artists do too |
| Packing 0.70 unreachable with 15–60 charts | CONFIRMED unreachable by any auto packer (reference charts auto-repack to 0.626); bar recalibrated to 0.50. Future work: rectangular chart synthesis / interlocking packer if texture memory ever demands ~0.75 |
| vt/v creeps past 2.0 | merge pass + boundary smoothing reduce seam length; bar re-check at U0 calibration |
| Per-chart unwrap flips on non-disk charts | disk invariant asserted at U1.4 with unit tests |

## 12. Definition of Done

1. `--uv-engine chart` default in the adaptive pipeline; organic kept as option;
   old `uv_agent` untouched and green.
2. Three budgets pass the §2 hard gates (stretch ≤ bar, packing ≥ 0.50 recalibrated,
   overlap, island band, no fallback). — DONE 2026-06-12, all three accepted.
3. Layout PNGs visually match the desired attachment style (user sign-off).
   — 2026-06-13: user reviewed t5850 vs reference side-by-side and REJECTED shape
   quality (jagged/tendril/concave charts, packing holes). U1.6 (§5b) round
   required; sign-off pending its results.
4. Consolidated final output folders + updated `docs/ADAPTIVE_LOWPOLY_RESULTS.md`.
5. Full test suite green (existing 240 + new chart tests).
