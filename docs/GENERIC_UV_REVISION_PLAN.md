# Generic UV Revision Plan

> **2026-06-13 STATUS — SUPERSEDED FOR THE USER'S ACTUAL GOAL by
> `docs/AUTO_ARTIST_UV_PLAN.md`.** This plan made the GENERIC, geometry-driven
> `chart` engine the default — correct for *valid* UVs, but it produces
> "random valid auto-pack" layouts, not *artist-style* part-readable UVs. The
> no-reference artist path now lives in `--uv-engine artist` (the `artist_uv_agent`
> package): semantic part segmentation → classification → seam templates → SLIM →
> layout grammar → density policy. `chart` REMAINS the stable generic fallback
> (`--uv-engine auto`/`chart`) and the per-part fallback the artist seam layer
> delegates to for `unknown`/`blob` parts. Everything below still describes the
> generic chart path accurately.

> Audience: another implementation session. This plan explains why the current
> reference-transfer UV path should not be the default for a general low-poly →
> UV tool, and lists the exact changes needed to make the generic path the
> product path.

## 1. Context

The adaptive low-poly pipeline currently has three P5 UV engines:

- `transfer`: reference-guided UV transfer (`transfer_uv_agent/`)
- `chart`: geometry-driven chart unwrap (`chart_uv_agent/`)
- `organic`: older cut-tree pelt unwrap (`uv_agent/blender/organic_unwrap.py`)

Recent work focused on `transfer` because the user compared the generated
`adaptive_t5850_uv.png` against `humanstatue_low.obj`'s artist UV. That reference
UV is semantic and part-based: head, arms, cloth strips, hands, and other body
parts have stable chart slots. The geometric engines cannot infer those exact
artist slots from geometry alone.

That led to the reference-guided engine. It projects chart ids from a UV'd
reference mesh, unwraps with those seams, then packs the result. After multiple
rounds, the final transfer implementation kept the transferred seams and
orientation but abandoned exact reference slot positions because the measured
requirements were jointly infeasible:

- exact-ish reference slots
- zero raster overlap
- packing >= 0.50
- uniform texel density

The final transfer path is acceptable only as an explicit reference-assisted
mode. It is not the right default for arbitrary objects.

## 2. Decision

For a general product path, do **not** default to `transfer`, even when a
reference object happens to have UVs.

Default P5 must be:

```text
adaptive low-poly mesh
  -> chart_uv_agent geometry segmentation
  -> SLIM unwrap
  -> average island scale
  -> Blender CONCAVE pack
  -> generic gates + checker render
```

`transfer` stays available only when the caller explicitly requests
`--uv-engine transfer` and understands that the input needs a compatible UV'd
reference.

## 3. Why

### 3.1 Transfer is not general

`transfer_uv_agent` assumes:

- there is a UV'd reference mesh,
- the generated mesh and reference mesh represent the same object,
- both are in the same world space,
- nearest-surface projection can map generated faces to reference chart ids.

Those assumptions are true for the `humanstatue_low.obj` comparison flow, but
false for the normal use case: arbitrary high-poly object with no artist UV.

If `auto` picks `transfer` just because `--reference` has UVs, a different asset
can inherit irrelevant chart topology and layout assumptions. That is the
failure mode the user is worried about.

### 3.2 The actual generic problem is chart generation

For no-reference UV generation, the solver is not the main issue. Blender SLIM,
`average_islands_scale`, and CONCAVE packing are reasonable building blocks.
The hard part is creating charts that are:

- connected topological disks,
- low-stretch / near-developable,
- not fragmented into confetti,
- compact enough to pack,
- semantically acceptable only in the generic sense: limbs/panels/creases tend
  to separate naturally, but exact artist slot positions are not promised.

That work belongs in `chart_uv_agent`, especially `segmentation.py`,
`shape_repair.py`, `pipeline.py`, and `gate.py`.

### 3.3 Current docs and comments are humanstatue-calibrated

Several thresholds and comments mention the humanstatue reference layout. That
was useful during calibration, but another implementer should not interpret
those as universal product truth. Generic gates should focus on measurable UV
quality, not similarity to a single reference.

## 4. Files To Modify

### 4.1 `worker/run_quad_retopo_job.py`

Current behavior:

```python
if engine == "auto":
    engine = "transfer" if ref_has_uv else "chart"
```

Required behavior:

```python
if engine == "auto":
    engine = "chart"
```

Also update the docstring around `run_p5_uv` so it says:

- `auto` means generic chart engine.
- `transfer` is explicit reference-guided mode only.
- `organic` remains comparison / legacy mode.

Do not remove `_run_p5_transfer`; just stop using it implicitly.

### 4.2 `chart_uv_agent/gate.py`

Convert the gate language from humanstatue-calibrated wording to generic
wording. Keep the same metrics, but make their purpose asset-agnostic:

- `overlap_ratio`
- `raster_overlap_ratio`
- `stretch_score`
- `packing_efficiency`
- `island_count`
- `small_island_ratio`
- `vt_v_ratio`
- `texel_density_variance`
- `uv_bounds`
- `fallback_used`
- chart shape gates: convexity, boundary smoothness, tendrils

Recommended first pass:

- keep current numeric values initially,
- rename comments from "reference artist UV" to "calibrated acceptance default",
- add TODO/report note that thresholds must be recalibrated on a multi-asset
  fixture set before production use.

Do not weaken overlap, bounds, or fallback gates.

### 4.3 `chart_uv_agent/pipeline.py`

This remains the main generic P5 pipeline.

Check and preserve these important behaviors:

- `segment(...)` produces initial connected charts.
- `repair_shapes(...)` tries to remove concave/tendril-like chart shapes.
- `tail_round(...)` records stuck charts instead of hiding them.
- `unwrap_and_pack(...)` uses SLIM (`MINIMUM_STRETCH`) by default.
- `correctness_pass(...)` owns the final UV and checks raster overlap.

The most important generic improvement area is not packing. It is the chart
segmentation / repair loop:

- reduce unnecessary chart fragmentation,
- avoid long tendrils,
- avoid non-disk charts,
- split only when stretch or self-overlap requires it,
- do not split merely to chase packing.

### 4.4 `chart_uv_agent/segmentation.py`

Review generic seam/chart rules:

- mandatory seams: boundary, non-manifold, and strong folds,
- split worst normal-cone charts only when necessary,
- diskify non-disk charts,
- absorb tiny charts,
- merge adjacent charts when the union is still a developable disk,
- straighten jagged boundaries.

The implementation direction is right for generic use. Do not add
humanstatue-specific anchors, body-part assumptions, or reference island slots.

### 4.5 `worker/run_quad_retopo_job.py` P6 outputs

For generic mode, the default review artifacts should be:

- generated UV layout PNG,
- generated checker front/side renders,
- `p5_gate.json`,
- exported OBJ/blend.

Reference UV side-by-side should be optional:

- keep it when a reference exists,
- label it as diagnostic,
- do not use it as the generic acceptance target.

## 5. Implementation Phases

### Phase G1 — Isolate transfer from auto

Goal: prevent accidental reference-specific behavior.

Tasks:

1. Change `run_p5_uv(..., engine="auto")` to resolve `auto -> chart`.
2. Update docstrings/log messages.
3. Add or update a unit test that verifies:
   - `auto` calls chart even when `ref` has UV layers,
   - explicit `transfer` still calls transfer / still errors loudly without UVs.

Acceptance:

- `--uv-engine auto` no longer enters `transfer_uv_agent`.
- Existing `--uv-engine transfer` behavior is unchanged.

### Phase G2 — Generic gate cleanup

Goal: stop implying that humanstatue reference similarity is the default
acceptance bar.

Tasks:

1. Rewrite `chart_uv_agent/gate.py` comments/docstring.
2. Keep gates strict for correctness:
   - raster overlap,
   - signed overlap,
   - UV bounds,
   - no fallback.
3. Keep packing and density gates, but describe them as generic quality gates.
4. Make `ChartGateConfig.to_dict()` include every active threshold, including
   raster overlap and shape gates, so reports are self-contained.

Acceptance:

- Gate JSON can be read without knowing the humanstatue reference story.
- No hard gate silently disappears.

### Phase G3 — Generic fixtures and regression set

Goal: validate that the generic engine behaves across object classes.

Create or reuse small Blender-free fixtures where possible:

- sphere / organic blob,
- humanoid-ish blob,
- cylinder / tube,
- cube or hard-surface boxy object,
- object with protrusions,
- object with thin panels or folds.

For each, assert:

- no raster overlap above threshold,
- UV bounds OK,
- no fallback,
- texel density variance reasonable,
- packing above threshold or failure reported honestly,
- no confetti explosion.

Do not use `humanstatue_low.obj` as the only calibration asset.

### Phase G4 — Checker render as generic deliverable

Goal: make visual density validation part of every P6 run.

The current worker already renders checker images. Ensure they are produced and
listed in the final report for generic chart mode:

- generated front checker,
- generated side checker,
- optional reference checker if a reference exists.

Acceptance:

- A reviewer can judge "uniform checker size" without opening Blender.

### Phase G5 — Documentation update

Goal: make the engine roles unambiguous.

Update:

- `docs/CHART_UV_AGENT_PLAN.md`: mark as generic default engine.
- `docs/UV_TRANSFER_PLAN.md`: mark as explicit reference-assisted mode, not
  default.
- `docs/ADAPTIVE_LOWPOLY_RESULTS.md`: distinguish generic chart results from
  reference-transfer experiments.

Acceptance:

- A new agent does not infer that transfer should be the default just because
  it was the most recent experiment.

## 6. Non-Goals

Do not do these in the generic revision:

- Do not force generated UV islands into `humanstatue_low.obj` slot positions.
- Do not use body-part names or human-specific anchors.
- Do not make `transfer` silently fall back to `chart`.
- Do not weaken overlap or bounds gates to make screenshots look better.
- Do not optimize for side-by-side similarity to one artist UV.

## 7. Suggested First Patch

The smallest useful patch is:

1. Change `auto -> chart` in `run_p5_uv`.
2. Update the `run_p5_uv` docstring.
3. Update `chart_uv_agent/gate.py` comments and `to_dict()`.
4. Add a test for auto engine selection.

After that, run:

```bash
uv run pytest tests/test_chart_uv_u*.py tests/test_uv_organic.py tests/test_smoothing_split.py
```

If Blender is available and time permits, run one P5 resume with:

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background \
  --python worker/run_quad_retopo_job.py -- \
  --from-phase P5 \
  --uv-engine auto \
  --target-faces 5850 \
  --out out/generic_uv_check/t5850
```

Verify `p5_gate.json` reports `"engine": "chart"`.

## 8. Expected Result

After this revision:

- General users get a geometry-based UV unwrap by default.
- Reference-transfer remains available for special review workflows.
- The checker render and gates evaluate UV usability rather than reference
  resemblance.
- Future quality work is concentrated in chart segmentation and generic
  fixtures, not in trying to make arbitrary objects inherit one reference layout.
