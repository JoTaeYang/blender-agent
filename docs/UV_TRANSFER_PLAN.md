# Reference-Guided UV Transfer — Implementation Plan (`--uv-engine transfer`)

> **2026-06-13 STATUS — THE REFERENCE-ASSISTED SPECIAL CASE.** Transfer is the
> path for "an artist UV of THIS object already exists somewhere"; for the
> *no-reference* artist-style goal use `--uv-engine artist`
> (`docs/AUTO_ARTIST_UV_PLAN.md`), which invents artist-style seams/layout from
> geometry alone. Transfer is unchanged below and remains explicit-only.
>
> **2026-06-13 STATUS — EXPLICIT REFERENCE-ASSISTED MODE, NOT THE DEFAULT.** Per
> `docs/GENERIC_UV_REVISION_PLAN.md`, transfer is selected **only** when the caller
> explicitly passes `--uv-engine transfer` AND supplies a UV'd `--reference` that
> represents the SAME object in the SAME world space. `--uv-engine auto` does NOT
> pick transfer, even when the reference has UVs — it resolves to the generic
> `chart` engine (`docs/CHART_UV_AGENT_PLAN.md`, the product default). Transfer
> fails loud without a UV'd reference; it never silently falls back to `chart`. Its
> assumptions (shared object/world-space, nearest-surface chart-id projection) are
> true for the humanstatue comparison flow but false for arbitrary assets, so it is
> a special review/comparison workflow, not the general path.

> Audience: implementation agent. This plan adds a THIRD P5 UV engine that
> transfers the reference asset's chart layout onto the adaptive low-poly mesh.
> Context: `docs/ADAPTIVE_LOWPOLY_PLAN.md` (pipeline), `docs/CHART_UV_AGENT_PLAN.md`
> (the geometric chart engine, now the generic default).

## 1. Why (team-lead review, 2026-06-13)

The chart engine (U0–U2.5) reached its geometric targets — stretch 0.2–0.3,
raster overlap ≤0.005, convex-ish charts, no fallback — but the user's team lead
rejected the result against the real bar: the layout is not *semantically*
similar to the reference. The reference UV is a **part-based design** (head,
arms, hands, torso, cloth strips as separate, well-oriented, stably-placed
charts); ours is geometry-driven (developability/convexity), which cannot
reproduce design intent by construction. Verdict: the problem is the **chart
generation criterion**, not the UV solver.

Decision: when a UV'd reference exists (this task), transfer its chart layout.
The geometric chart engine remains the no-reference mode.

**Scope note.** `humanstatue.obj` (the actual pipeline input) has NO UVs, so
"preserve UVs through decimation" is impossible — transfer-from-reference is the
only way to inherit the artist design. This engine requires `--reference` with
vt data; it must fail loudly (not fall back silently) if the reference has no UVs.

## 2. Goal

`out/chart_acc/t<N>/` layouts where each chart corresponds to a reference chart
(same body part, same approximate UV position/orientation/scale), reviewable
side-by-side: "head chart is where the head chart is". Hard correctness gates
(overlap, bounds) unchanged.

## 3. Algorithm (P5, runs in Blender; meshes share world space — verified P0)

### T1 — Reference chart extraction
1. Load `humanstatue_low.obj` (3,191 v / 5,850 f, 39 UV islands, 51 shells).
2. Build UV islands from its vt data (reuse `uv_islands_from_uvmap` in
   `uv_agent/geometry/evaluation.py`): face → `ref_chart_id`.
3. Per chart, record: face set, UV bbox (center, extents), principal UV axis
   (PCA of the chart's UV coords), mean texel density (UV area / 3D area).
4. Build a BVH over the reference triangles (`mathutils.bvhtree.BVHTree`)
   carrying face → chart id.

### T2 — Chart-id projection onto the adaptive mesh
For each adaptive face: sample its centroid (optionally + corners, majority
vote), query nearest reference surface point via the BVH, copy that reference
face's `ref_chart_id`.

**Pitfall guards (mandatory, all three):**
- **Normal compatibility**: reject a nearest-hit whose reference face normal has
  `dot(n_adaptive, n_ref) < 0.2` with the adaptive face normal; take the nearest
  *compatible* hit instead (k-nearest fallback, k≤8). This prevents chart bleed
  at contact regions (between legs, arm–torso, cloth layers — the reference's
  overlapping separate shells).
- **Distance sanity**: hits farther than ~2× the adaptive mesh's mean edge
  length are "unassigned"; fill them afterwards from face-adjacency majority.
- **Speckle cleanup**: after projection, run label smoothing — iterate
  (≤10 rounds): any face whose chart id disagrees with the majority of its
  edge-neighbors flips to that majority. Then enforce **each chart id =
  one connected component**: minor disconnected fragments (< 20% of that
  chart's faces) are absorbed into their surrounding chart; a major split
  (two big components with the same id, e.g. left/right legs that the
  reference maps to mirrored charts) becomes two charts inheriting the same
  reference placement slot with an offset.

### T3 — Seams + unwrap
1. Mark every adaptive edge whose two faces have different chart ids as a seam.
   Also keep mesh-boundary edges as seams (none expected; mesh is watertight).
2. Per-chart disk check (Euler χ=1, reuse `chart_uv_agent/segmentation.py`
   helpers); a non-disk chart gets the minimal extra cut (existing diskify code).
3. Unwrap the whole mesh honoring seams with SLIM
   (`uv.unwrap(method='MINIMUM_STRETCH')` — locally injective, §5d finding);
   ABF fallback per chart only if SLIM errors on it.

### T4 — Reference-guided placement (NOT pack_islands)

> **SUPERSEDED 2026-06-13 (round 3, measured dead-ends).** Slot-anchored placement is
> jointly infeasible with {overlap 0, packing ≥ 0.50} on blob charts, proven by four
> mechanisms tried on the 5,850 mesh: density-clamp (round 2: packing 0.029 collapse),
> capped separation (raster overlap 0.05–0.14 residual — the reference slots' bboxes
> themselves interlock), rect occupancy first-fit (packing 0.25), true-shape mask
> first-fit + gravity compaction (packing 0.32–0.37). FINAL DESIGN: semantic
> correspondence lives in the transferred SEAMS (T2/T3 — every chart IS a reference
> part); per-part reference ORIENTATION is kept (rotation alignment, 4-way IoU);
> position/packing is delegated to Blender's shape-aware CONCAVE packer with
> `rotate=False` (preserves the aligned orientations, guarantees overlap-free, packing
> 0.56–0.60). Slot-position IoU is reported honestly (~0.01 — positions differ from the
> artist tile) — the part-for-part chart structure, not the tile coordinates, is what
> survived review as the semantic content. Gate parity adds `packing_efficiency ≥ 0.50`
> HARD (its absence let round 2 ship a collapsed layout as "accepted").
For each adaptive chart, place its UVs into the matching reference chart's slot:
1. Scale: match the reference chart's texel density (adaptive chart UV area /
   3D area == reference's), then clamp so the chart fits its reference bbox
   slot with the slot's margin.
2. Rotation: align the chart's principal UV axis to the reference chart's.
   Try the 4 axis-aligned flips/rotations and keep the one maximizing IoU with
   the reference chart's UV footprint (cheap raster at 256²).
3. Translation: reference chart's bbox center.
4. After placement: full-layout raster overlap check (existing
   `raster_overlap_ratio`). Any colliding pair → shrink the smaller chart
   within its slot (up to −15%); if still colliding, nudge along the slot's
   free direction; last resort, fall back to `pack_islands` for the colliding
   charts ONLY, inside the union of their slots. Record every adjustment.

### T5 — Gate + report
| check | bar | type |
|---|---|---|
| raster_overlap_ratio | ≤ 0.005 | HARD (unchanged) |
| flipped-area overlap_ratio | ≤ 0.001 | HARD (unchanged) |
| uv bounds [0,1] | all in | HARD |
| fallback_used (Smart-UV) | false | HARD |
| chart_count | ≈ reference's 39 ± the T2 split/absorb delta; report | report |
| chart correspondence | every adaptive chart maps to exactly one ref chart id; coverage table (ref charts with no adaptive faces are listed) | report |
| placement IoU | mean IoU(adaptive chart footprint, ref chart footprint); no invented bar — measure and report round 1, calibrate after review | report |
| stretch_score | report (SLIM should land ≤ the chart engine's 0.2–0.3; investigate if >0.5) | report |
| texel_density_variance | report | report |

The acceptance bar that matters: **side-by-side PNG review by the user/team
lead** — part correspondence must be visible. Metrics exist to automate later.

## 4. Integration

- `transfer_uv_agent/` new package (sibling; reuse `uv_agent` evaluation +
  `chart_uv_agent` disk/segmentation helpers via imports, do not fork).
- `worker/run_quad_retopo_job.py`: `--uv-engine transfer` (new default when
  `--reference` has UVs; engines `chart` and `organic` unchanged and selectable).
- No reference UVs + `--uv-engine transfer` → hard error with a clear message
  (never silently switch engines).

## 5. Tests

- Unit (Blender-free): chart extraction from a synthetic OBJ with 2 UV islands;
  label smoothing fixes injected speckle; connected-component enforcement;
  normal-compatibility rejection (two parallel close planes with opposite
  normals must not bleed); placement math (scale/rotation/IoU selection).
- Headless integration: decimated copy of a UV'd fixture (sphere with 2 charts)
  → transfer → gates green, chart ids match by construction.
- Full suite stays green (290 + new).

## 6. Acceptance

Re-run P5+P6 on the three A4 meshes (resume from `.blend`, do NOT redo P1–A4)
with `--uv-engine transfer`. Deliver per budget in `out/transfer_acc/t<N>/`:
OBJ (v/vt/vn), blend, layout PNG, **side-by-side PNG (ours | reference)**,
checker render, `p5_gate.json` with the T5 table + every T4 adjustment logged.

Definition of done:
1. Three budgets: HARD gates green, no fallback.
2. Side-by-side PNGs show part-level correspondence with the reference layout
   (head/arms/torso/cloth strips in matching positions) — user & team-lead
   sign-off is the bar.
3. Chart engine and organic engine still selectable and green (no regression).
4. `docs/ADAPTIVE_LOWPOLY_RESULTS.md` gains a "UV transfer" section with the
   correspondence table and before/after layout comparison.

## 7. Risks

| risk | mitigation |
|---|---|
| Chart bleed at contacts (legs, arm–torso, layered cloth) | normal-compatibility + distance guards (T2); visual seam-overlay render in report |
| Reference charts with no adaptive counterpart (tiny detail shells lost in decimation) | allowed; listed in the coverage table; their slot stays empty |
| Adaptive chart larger than its reference slot (decimation changed proportions) | density-then-clamp scaling (T4.1); per-chart scale deviation logged |
| Speckled seams → jagged charts | label smoothing + connectivity enforcement are mandatory, with unit tests |
| SLIM failure on a transferred chart | per-chart ABF fallback (logged), then raster gate still applies |
| This engine is asset-specific by design | documented: transfer = reference-guided mode; chart engine remains the no-reference path |
