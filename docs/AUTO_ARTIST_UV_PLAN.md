# Auto Artist-Style UV Plan

> **2026-06-13 STATUS — IMPLEMENTED (AR1–AR6), AR7 calibration pending.** The
> `artist_uv_agent/` package and `--uv-engine artist` are live (worker
> `_run_p5_artist`). Done: AR1 scaffold + wiring, AR2 segmentation
> (`segmentation.py`, barrier-geodesic watershed), AR3 descriptors + classification
> (`descriptors.py`/`classification.py`), AR4 seam templates (`seams.py`, with a
> seam-aware disk test + per-part chart fallback), AR5 layout grammar (`layout.py`,
> pure band/group shelf pack — overlap-free, uniform density), AR6 density policy
> (`density.py`), gate + report (`gate.py`), debug overlays (`debug.py`). 43
> Blender-free unit tests + a passing Blender P5-resume smoke on `humanstatue`
> (gate ACCEPTED; all HARD gates pass; results in `ADAPTIVE_LOWPOLY_RESULTS.md`).
> NOT default (still `--uv-engine chart`).
>
> **2026-06-13 REVISION (user correction).** The band/shelf BBOX layout wrecked UV space
> (packing 0.24, tile half-empty) and trident UVs came out as blobs. Fixes: (1) band/shelf
> demoted to DEBUG-only — final layout is the Blender **CONCAVE packer** (+ a long-island
> orientation pass kept only if packing holds); grouping/bands are now report-only
> metadata. (2) **`packing_efficiency` promoted to HARD** (floor 0.40). (3) A dedicated
> **cylinder template** (`seams.cylinder_template`: end-cap separation + lengthwise cut)
> flattens tubes into RECTANGLES; (4) a HARD **`cylinder_rectangular`** gate fails any
> cylinder that stays a blob/fragment. Re-run on humanstatue t5850: packing **0.55**,
> stretch **0.48**, `cylinder_blob_count 0`, trident shaft = a 6.6:1 rectangle + cap, gate
> ACCEPTED with NO hard AND NO quality fails.
>
> **Branch segmentation IMPLEMENTED** (`segmentation.split_branched_parts`, axis
> cross-section sweep): splits a tube-like part at a multi-prong fork into shaft + prongs,
> but ONLY where an end region DISCONNECTS into ≥ N components — so it splits genuine forks
> (a real humanstatue fork dropped stretch 0.48 → 0.075) and never imposes arbitrary cuts.
> The trident's own 3 prongs are SOLID CONNECTED geometry at 5756 AND 10k faces (end region
> stays one component — joined by head/webbing, no gaps), so they are correctly NOT split;
> the trident flattens to one 6.6:1 rectangular strip (passes the no-blob gate). Per-tine
> separation is not possible without arbitrary cuts at these resolutions — the tine geometry
> isn't there. **Remaining:** AR7 multi-asset calibration (e.g. `convexity_p10` ≈ 0.40 from
> thin prong charts). Deferred: mirrored symmetric pairing (UV mirror flips winding).

> Audience: implementation agent. This plan defines a new no-reference UV engine
> whose goal is not merely "valid generic UVs", but artist-style UVs: readable
> semantic parts, purposeful seams, consistent orientation, layout grammar, and
> checker-friendly texel density. This is the path to pursue when no UV'd
> reference asset exists.

## 1. Decision

The user needs **artist-style UVs without a reference UV**.

Do not continue treating this as a `chart_uv_agent` threshold-tuning problem.
`chart_uv_agent` is useful as a lower-level geometric unwrap engine, but it
does not know what a "head", "limb", "cloth strip", "cap", "shaft", "panel",
or "left/right pair" is. Artist UVs require a semantic layer above geometric
charting.

Create a new engine:

```text
--uv-engine artist
```

High-level pipeline:

```text
low-poly mesh
  -> semantic part segmentation
  -> part classification
  -> seam templates per part type
  -> unwrap with SLIM
  -> layout grammar / orientation / grouping
  -> importance-based texel density
  -> final pack + checker/gate/report
```

Keep existing engines:

- `chart`: generic geometry-driven UV, good for validity and checker uniformity.
- `transfer`: explicit reference-assisted mode, good only when a compatible UV'd
  reference exists.
- `organic`: legacy comparison path.

`artist` should eventually become the default when the product goal is
human-editable, part-readable UVs.

## 2. What "Artist-Style" Means

The target is not exact imitation of `humanstatue_low.obj`. The target is a
layout a texture artist can understand and edit.

Required properties:

1. **Semantic islands**
   - islands correspond to object parts, not arbitrary developability blobs;
   - protrusions, caps, panels, limbs, cloth sheets, handles, and thin strips are
     recognizable.

2. **Purposeful seams**
   - seams prefer hidden/back-facing/concave/crease lines;
   - cylindrical parts open along one long back seam;
   - flat panels stay mostly intact;
   - caps separate from tubes when appropriate.

3. **Layout grammar**
   - long cloth/panel parts become vertical or horizontal strips;
   - cylindrical limbs/shafts become long rectangles plus cap islands;
   - left/right symmetric parts are paired, same scale, same orientation;
   - detail islands group near their parent part;
   - major regions occupy predictable bands, not random scatter.

4. **Texel-density control**
   - default uniform checker size;
   - optional importance weights for face/head/hands/front-facing parts;
   - density changes must be intentional and reported.

5. **Editable output**
   - UV should be usable as a starting point for manual editing;
   - charts should be readable in a 2D UV editor;
   - avoid microscopic confetti and needle-like islands.

## 3. Why Existing Engines Are Insufficient

### 3.1 `chart`

`chart_uv_agent` produces valid UVs by geometric criteria:

- low stretch,
- low overlap,
- reasonable packing,
- uniform checker density.

But the recent `out/generic_run/t5850` result showed the gap:

- metrics were mostly good:
  - `stretch_score`: 0.31766
  - `raster_overlap_ratio`: 0.00139
  - `texel_density_variance`: 0.000101
  - `packing_efficiency`: 0.445935
  - `island_count`: 43
- checker renders were fairly uniform;
- yet the UV layout looked like random geometric fragments, not artist planning;
- `convexity_p10` failed and several stuck charts remained;
- semantic readability was weak.

Conclusion: charting alone solves "valid UV", not "artist UV".

### 3.2 `transfer`

`transfer_uv_agent` needs a compatible UV'd reference. It cannot solve the
no-reference case. It is useful for review workflows where the desired UV design
already exists somewhere else.

### 3.3 `organic`

The old pelt-style organic engine makes too few large islands and cannot reach
the desired part-based decomposition.

## 4. Proposed Package

Add a new package:

```text
artist_uv_agent/
  __init__.py
  pipeline.py              # A0-A7 orchestration
  segmentation.py          # semantic part segmentation
  descriptors.py           # per-part geometry descriptors
  classification.py        # part type labels
  seams.py                 # seam templates by part type
  layout.py                # layout grammar, grouping, orientation
  density.py               # importance and texel-density policy
  gate.py                  # artist UV gate + report
  debug.py                 # SVG/PNG/debug table helpers
tests/test_artist_uv_*.py
```

Reuse existing code where possible:

- `uv_agent.geometry.mesh_graph`
- `chart_uv_agent.unwrap.unwrap_and_pack`
- `chart_uv_agent.unwrap.repack`
- `chart_uv_agent.segmentation.flood_charts`
- `uv_agent.geometry.evaluation`
- `uv_agent.blender.organic_unwrap.read_uvmap`
- Blender's SLIM unwrap and CONCAVE packer

Do not fork low-level UV readers/writers unless unavoidable.

## 5. Pipeline

### A0 — Inputs and Assumptions

Input at P5:

- low-poly mesh from adaptive retopo,
- no required UV reference,
- optional camera/view direction,
- optional object category hint (`humanoid`, `prop`, `hard_surface`,
  `cloth`, `unknown`),
- optional importance hints.

The first version must work without category hints, but hints can improve
layout grammar later.

### A1 — Semantic Part Segmentation

Goal: split the mesh into meaningful 3D parts before UV charting.

Use graph-based segmentation over faces/vertices. Candidate signals:

- concave creases and high dihedral valleys,
- convex protrusion necks,
- geodesic extremities,
- local thickness / radius changes,
- curvature clusters,
- connected components / shells,
- symmetry pairs,
- approximate skeleton branches,
- panel-like flat regions.

Implementation approach:

1. Build a mesh graph with face adjacency.
2. Detect strong boundary candidates:
   - concave edges,
   - sharp creases,
   - narrow neck loops,
   - high curvature ridges.
3. Find protrusion tips via geodesic farthest-point sampling.
4. Grow regions from tips and core seeds with boundary costs:
   - cheap to stop at concave/sharp/narrow-neck edges,
   - expensive to cut through smooth continuous surfaces.
5. Merge tiny regions into neighboring parent regions.
6. Output `Part` records:
   - `part_id`
   - `face_ids`
   - adjacency to other parts
   - boundary edges
   - confidence score

First target: stable part decomposition, not perfect labels.

### A2 — Part Descriptors

For each part, compute descriptors:

- area,
- bounding box extents,
- principal axes,
- geodesic length / width,
- thickness estimate,
- curvature statistics,
- boundary loop count,
- genus/disk likelihood,
- front/back visibility,
- symmetry candidate id,
- attachment parent,
- extremity score,
- flatness / cylindricalness / stripness.

These descriptors drive both classification and layout.

### A3 — Part Classification

Assign each part a coarse type:

```text
panel       flat or cloth-like area
strip       long thin cloth/panel section
cylinder    limb, handle, shaft, tine
cap         end of a cylinder or round protrusion
blob        head/torso/organic mass
detail      small attached feature
shell       detached or weakly attached component
unknown     fallback
```

This is not semantic naming like "left arm" or "head" yet. It is geometry class.
Artist-style UV mostly needs geometry class + grouping, not perfect object
recognition.

Classification should be rule-based in v1, with optional ML later.

### A4 — Seam Templates

Generate seams from part type.

Template rules:

- `cylinder`
  - one long seam on hidden/back side or least visible side;
  - separate caps when boundary/cap-like faces exist;
  - unwrap body as a long rectangle.

- `strip`
  - preserve as one long island when stretch allows;
  - seam along one long side or existing boundary;
  - orient with length vertical or horizontal.

- `panel`
  - keep mostly intact;
  - cut only at concave folds or non-disk topology;
  - prefer straight boundary loops.

- `blob`
  - use a pelt seam tree through back/concave lines;
  - split only when stretch or self-overlap requires it;
  - avoid random many-piece fragmentation.

- `detail`
  - small self-contained island;
  - group near parent part in layout.

- `unknown`
  - fall back to `chart_uv_agent` segmentation for that part only.

Output:

- seam edge set,
- `part_id -> chart ids`,
- per-chart intended layout role.

### A5 — Unwrap

Use Blender SLIM:

```python
bpy.ops.uv.unwrap(method="MINIMUM_STRETCH", margin=...)
```

Then:

- average island scale,
- evaluate stretch/overlap,
- if a part fails:
  - add template-specific relief seam,
  - re-unwrap that part,
  - keep a repair history.

Avoid global Smart UV fallback as a shipped output.

### A6 — Layout Grammar

This is the main difference from `chart`.

Do not rely only on Blender's global packer to decide the final visual layout.
Apply grammar first, then pack/refine.

Suggested layout bands:

```text
top band:     small details / caps / head-like blobs
middle band:  torso/blob/panel major parts
bottom band:  long strips / cloth / limbs / shafts
side bands:   symmetric paired parts or secondary details
```

Rules:

- orient long islands by principal axis;
- pair symmetric parts side-by-side with matched scale and mirrored orientation;
- place child/detail islands near parent part;
- group charts by part, with small local padding;
- keep strip islands parallel and aligned;
- use packer inside groups first, then pack groups globally;
- allow some packing inefficiency to preserve readability.

Implementation stages:

1. Normalize UV density globally.
2. Rotate islands to grammar orientation.
3. Build `LayoutGroup` objects:
   - one major group per semantic part,
   - child details attached to parent group.
4. Pack islands inside each group.
5. Pack groups into bands.
6. Run final overlap/bounds repair.

Important: readability wins over maximum packing. A 0.45 readable layout is
better than a 0.60 random scatter layout for this engine.

### A7 — Density / Importance Policy

Default:

- global uniform texel density.

Optional weights:

- front-facing visible parts: 1.1x
- face/head/hands or small details: 1.2x
- hidden/back underside: 0.8x

In v1, do not guess aggressive importance. Keep weights near 1.0 unless the
classification confidence is high.

Report:

- density variance,
- per-part density,
- any intentional density weights.

## 6. Gates

Separate hard correctness gates from artist-style review gates.

### Hard Gates

Must pass:

- `uv_bounds_ok == true`
- `fallback_used == false`
- `raster_overlap_ratio <= 0.005`
- `overlap_ratio <= 0.001`
- `texel_density_variance <= threshold`
- no island below minimum face/area unless marked `detail`

### Quality Gates

Should pass, but may be calibrated:

- `stretch_score <= 0.5` initially
- `packing_efficiency >= 0.38` initially for artist mode
- `island_count <= dynamic limit`
- `vt_v_ratio <= 2.2`
- no tendril islands
- chart convexity / boundary smoothness above calibrated floor

### Artist-Style Report Metrics

New metrics:

- `part_coverage`: fraction of faces assigned to a semantic part
- `part_confidence_mean`
- `charts_per_part`
- `symmetry_pair_count`
- `paired_scale_error`
- `layout_group_count`
- `strip_alignment_score`
- `detail_near_parent_score`
- `orientation_consistency`
- `readability_score` (aggregate, report-only in v1)

Do not make `readability_score` hard until it has been calibrated on several
assets.

## 7. Required Outputs

For every `--uv-engine artist` run:

```text
out/.../
  p5_gate.json
  artist_parts.json
  artist_layout.json
  adaptive_t<N>_uv.png
  adaptive_t<N>_uv_colored_by_part.png
  adaptive_t<N>_checker_front.png
  adaptive_t<N>_checker_side.png
  adaptive_t<N>_part_debug_front.png
  adaptive_t<N>_part_debug_side.png
```

Debug overlays are mandatory. Without them, reviewing semantic segmentation is
guesswork.

`artist_parts.json` should include:

- part id,
- type,
- face count,
- area,
- confidence,
- parent id,
- symmetry mate id,
- chart ids,
- repair history.

## 8. Integration

### Worker

Update `worker/run_quad_retopo_job.py`:

```python
if engine == "artist":
    return _run_p5_artist(bpy, low, ref, out_dir)
```

Do not make `artist` default until it has a fixture suite and at least one full
acceptance run. During development:

- `--uv-engine chart`: stable generic fallback
- `--uv-engine transfer`: explicit reference-assisted mode
- `--uv-engine artist`: new target path

After acceptance, consider:

```text
auto -> artist
```

only if artist mode is robust across the fixture set.

### Docs

Update:

- `docs/GENERIC_UV_REVISION_PLAN.md`: mark as superseded for the user's actual
  goal; generic chart remains fallback.
- `docs/CHART_UV_AGENT_PLAN.md`: chart is lower-level fallback / component.
- `docs/UV_TRANSFER_PLAN.md`: reference-assisted special case.
- `docs/ADAPTIVE_LOWPOLY_RESULTS.md`: add artist-engine experiments separately.

## 9. Implementation Phases

### Phase AR1 — Scaffold

Create `artist_uv_agent/` package and wire `--uv-engine artist`.

Acceptance:

- engine runs and delegates to chart fallback unchanged;
- outputs identify engine as `artist`;
- tests verify mode selection.

### Phase AR2 — Part Segmentation v1

Implement graph segmentation:

- concave/crease boundary candidates,
- extremity seeds,
- region growing,
- tiny part merge,
- debug colors.

Acceptance:

- `artist_parts.json` exists;
- part debug render shows stable major parts;
- no UV behavior changed yet.

### Phase AR3 — Part Classification v1

Implement descriptors and rule-based type labels.

Acceptance:

- major parts get plausible labels (`blob`, `strip`, `cylinder`, `panel`,
  `detail`, `unknown`);
- confidence is reported;
- unknown fallback is explicit, not silent.

### Phase AR4 — Seam Templates v1

Generate seams by part type, falling back to chart segmentation inside unknown
or failing parts.

Acceptance:

- generated UV islands correspond visibly to parts;
- no raster overlap;
- checker still reasonable.

### Phase AR5 — Layout Grammar v1

Implement group-based layout:

- orient by type,
- group child details near parents,
- align strips,
- pair symmetric parts,
- pack groups into bands.

Acceptance:

- UV layout is visibly more organized than `chart`;
- packing may be lower than chart but should stay above initial artist floor;
- side-by-side with chart output shows better readability.

### Phase AR6 — Density / Importance v1

Add near-uniform density with optional small weights.

Acceptance:

- checker size remains coherent;
- density differences are intentional and reported.

### Phase AR7 — Calibration and Acceptance

Run at least:

- `humanstatue` 2.9k / 5.85k / 10k,
- sphere/blob fixture,
- cylinder/tube fixture,
- flat panel/cloth-like fixture,
- hard-surface box fixture,
- protrusion fixture.

Acceptance:

- hard gates pass;
- generated UV is visually more artist-readable than chart mode;
- checker is acceptable;
- failures include actionable debug reports.

## 10. First Implementation Target

Do not try to solve every object category at once.

First target:

```text
organic humanoid / statue-like mesh with cloth and protrusions
```

Reason:

- this is the current user asset class;
- it contains blobs, cylinders, strips, and details;
- success here exercises most of the artist UV concepts.

The v1 can explicitly say:

```text
artist engine supports organic/statue-like assets best;
hard-surface support is experimental.
```

## 11. Non-Goals

Do not do these in v1:

- no deep-learning semantic segmentation dependency;
- no hardcoded `humanstatue` body-part labels;
- no exact imitation of `humanstatue_low.obj` slot coordinates;
- no silent fallback to Smart UV;
- no optimizing only for packing efficiency;
- no lowering overlap/bounds gates for prettier screenshots.

## 12. Key Engineering Risks

### Risk: semantic segmentation is unstable

Mitigation:

- keep confidence scores;
- expose debug overlays;
- fallback per part to chart segmentation;
- avoid making low-confidence labels drive aggressive layout choices.

### Risk: layout grammar creates overlap or poor packing

Mitigation:

- pack inside groups first;
- keep a final overlap repair pass;
- allow group-level repack while preserving orientation;
- accept lower packing for readability, but report it.

### Risk: too many special cases

Mitigation:

- classify by geometry type, not object name;
- rules should apply to cylinders/panels/blobs/strips generally.

### Risk: artist score is subjective

Mitigation:

- keep hard technical gates separate;
- report visual debug artifacts;
- calibrate readability metrics after several reviewed assets.

## 13. Suggested First Patch

1. Add `artist_uv_agent/` scaffold.
2. Add `--uv-engine artist` branch in `worker/run_quad_retopo_job.py`.
3. Implement `artist` as wrapper around chart mode initially.
4. Emit `artist_parts.json` with one `unknown` part covering the mesh.
5. Add tests for mode selection and output schema.

Then implement AR2 segmentation.

Initial test command:

```bash
uv run pytest tests/test_chart_uv_u*.py tests/test_uv_organic.py
```

Blender smoke:

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background \
  --python worker/run_quad_retopo_job.py -- \
  --from-phase P5 \
  --uv-engine artist \
  --target-faces 5850 \
  --out out/artist_smoke/t5850
```

## 14. Expected Outcome

When this plan succeeds, UVs should no longer look like random valid auto-pack
layouts. They should read like:

- "these are cloth strips",
- "these are cylindrical limbs/shafts",
- "these are caps/details",
- "these two islands are a symmetric pair",
- "details are near their parent part",
- "checker density is coherent".

That is the no-reference version of artist UV.
