# Minimal-Island Distortion-Constrained UV Plan

> Audience: implementation session taking over the UV work. This plan reflects
> the user's latest acceptance criteria and supersedes the recent semantic
> `artist_uv_agent` direction for the main product path.

## 1. User Criteria

The user reported that the real end user cares most about these three rules:

1. **Keep UV island count as low as possible.**
2. **Every model edge bent by 90 degrees or more must become a UV seam.**
3. **After checkerboard mapping, if checker distortion exceeds a threshold,
   increase the UV island count only as needed.**

In other words:

```text
minimize island_count
subject to:
  - all dihedral >= 90deg edges are protected seams
  - checker/stretch distortion <= threshold
  - no UV overlap
  - UVs remain in [0,1]
  - no Smart UV fallback as shipped output
```

This is **not** primarily a semantic artist-layout problem. It is a
distortion-constrained minimal charting problem.

## 2. Decision

Do **not** create another new agent.

Use and revise the existing:

```text
chart_uv_agent/
```

Reason:

- `chart_uv_agent` already has the right core architecture:
  - mandatory seams,
  - chart flood fill,
  - split/merge loop,
  - SLIM unwrap,
  - stretch measurement,
  - raster overlap check,
  - packing and gate.
- The latest user criteria match `chart_uv_agent` better than
  `artist_uv_agent`.
- Creating a new package would duplicate tested code and add confusion.

`artist_uv_agent` should be treated as an experiment / optional mode. It should
not drive the main acceptance path for these criteria.

## 3. Important Context

Recent work tried three directions:

### 3.1 Reference Transfer

`transfer_uv_agent` was built to make generated UVs resemble
`humanstatue_low.obj`'s reference UV. It requires a compatible UV'd reference.
It is not general and should stay explicit:

```text
--uv-engine transfer
```

### 3.2 Generic Chart

`chart_uv_agent` produced valid, fairly uniform UVs in:

```text
out/generic_run/t5850/
```

Representative metrics:

```text
engine: chart
island_count: 43
stretch_score: 0.31766
raster_overlap_ratio: 0.00139
texel_density_variance: 0.000101
packing_efficiency: 0.445935
vt_v_ratio: 1.309
```

The layout did not look like the reference artist UV, but the checker and
technical metrics were mostly acceptable. This is closest to the user's latest
requirements.

### 3.3 Semantic Artist Engine

`artist_uv_agent` attempted semantic part segmentation and layout grammar. It
improved after several fixes, but it solves a different problem:

- part grouping,
- cylinder templates,
- trident/fork handling,
- visual layout readability.

The latest user criteria no longer prioritize semantic grouping. They prioritize
minimal islands and threshold-based distortion splitting. Therefore do not keep
chasing semantic layout for the main path.

## 4. Target Behavior

The UV engine should behave like this:

1. Start with the fewest charts compatible with mandatory seams.
2. Unwrap.
3. Measure checker/stretch distortion.
4. If all distortion is below threshold, stop.
5. If not, split only the worst offending island.
6. Repeat until the threshold passes or a strict cap is reached.

Island count should increase only for:

- mandatory 90-degree seams,
- non-disk topology,
- self-overlap / raster overlap,
- checker/stretch distortion above threshold.

Do not increase island count for:

- semantic part grouping,
- trying to resemble a reference UV,
- layout aesthetics,
- convexity alone unless it causes packing/overlap/distortion failure.

## 5. Files To Focus On

### 5.1 `chart_uv_agent/segmentation.py`

Already contains:

- `mandatory_seam_edges(mesh, fold_angle=90.0)`
- `segment(...)`
- `split_chart(...)`
- diskify
- absorb tiny charts
- merge pass

Required changes:

1. Make 90-degree seams visibly protected in code and logs.
2. Ensure mandatory seams are never removed by:
   - absorb,
   - merge,
   - straighten,
   - repair passes.
3. Bias segmentation toward fewer charts:
   - start from mandatory seams,
   - merge aggressively where distortion permits,
   - avoid pre-emptive splitting by visual shape criteria.

### 5.2 `chart_uv_agent/pipeline.py`

Already unwraps, evaluates, and refines.

Required changes:

1. Make the refinement loop explicitly distortion-driven.
2. Use per-face/per-island stretch to choose the split target.
3. Split exactly one worst island per refinement round.
4. Stop immediately when distortion threshold passes.
5. Do not split for packing alone.
6. Do not split for convexity alone unless it is tied to a hard failure.

Important existing helpers:

- `per_face_stretch(...)`
- `_worst_stretch_chart(...)`
- `raster_overlap_diagnosis(...)`
- `split_chart(...)`
- `correctness_pass(...)`

### 5.3 `chart_uv_agent/gate.py`

Reframe the gate around the user's rules.

Hard gates:

- all 90-degree seams respected,
- `raster_overlap_ratio <= max`,
- `overlap_ratio <= max`,
- `stretch_score <= max`,
- `texel_density_variance <= max`,
- `uv_bounds_ok == true`,
- `fallback_used == false`.

Optimization/report:

- island count,
- packing efficiency,
- vt/v,
- convexity,
- boundary smoothness.

`convexity_p10` should not block shipping by itself unless the user explicitly
cares about island shape aesthetics. The user's latest statement did not mention
convexity; it mentioned distortion.

### 5.4 `worker/run_quad_retopo_job.py`

Make sure the main path uses:

```text
--uv-engine chart
```

or `auto -> chart` for this acceptance track.

Do not use `artist` as the default for the latest criteria.

## 6. Proposed Algorithm

### Phase M1 — Define Distortion Metric

Use existing area/stretch metric first:

- global `stretch_score`
- per-face stretch from `per_face_stretch`
- per-island aggregate stretch
- texel density variance

Add explicit checkerboard distortion naming in reports so users can connect the
metric to visual checker results.

Report:

```json
{
  "checker_distortion_score": ...,
  "worst_island_distortion": ...,
  "worst_island_id": ...,
  "worst_face_count": ...
}
```

Implementation note: this can initially alias existing stretch metrics; do not
invent a separate raster checker analysis unless needed.

### Phase M2 — Mandatory 90-Degree Seam Audit

Add a report/check proving all `dihedral >= 90` edges are seams in the final
layout.

Required JSON fields:

```json
{
  "mandatory_90_edges": 123,
  "mandatory_90_missing": 0
}
```

Gate:

```text
mandatory_90_missing == 0
```

### Phase M3 — Minimal Initial Segmentation

Start from mandatory seams and only split enough to make charts valid disks.

Avoid running shape-repair/tail-round as hard preprocessing unless it is proven
needed for distortion/overlap. If those passes remain, make them optional or
report-only.

Goal:

```text
initial_island_count as low as possible
```

### Phase M4 — Distortion-Driven Refinement Loop

Pseudo-code:

```python
seams = mandatory_seams + diskify_minimal_cuts

for round in range(max_rounds):
    unwrap_and_pack(seams)
    metrics = evaluate()

    if overlap failure:
        split or repair overlap offender
        continue

    if checker/stretch <= threshold:
        break

    worst = island_with_highest_distortion()
    add_one_split(worst)
```

Key rule:

```text
one split must have one reason, recorded in history
```

History example:

```json
{
  "round": 3,
  "action": "split",
  "reason": "checker_distortion",
  "island": 12,
  "before_distortion": 0.72,
  "after_round_islands": 18
}
```

### Phase M5 — Stop Conditions

Stop when:

- distortion passes,
- overlap passes,
- UV bounds pass,
- no fallback used.

Do not continue splitting to improve:

- reference similarity,
- semantic grouping,
- convexity,
- visual layout aesthetics.

### Phase M6 — Checker Deliverables

Every run must output:

- UV layout PNG,
- checker front render,
- checker side render,
- `p5_gate.json`,
- distortion history.

The final report should explicitly state:

```text
islands increased from N0 to N because checker distortion exceeded threshold.
```

or:

```text
islands stayed at N because checker distortion was within threshold.
```

## 7. Gate Changes

Suggested `ChartGateConfig` direction:

```python
class ChartGateConfig:
    fold_angle_mandatory = 90.0
    stretch_max = 0.5              # calibrate
    raster_overlap_max = 0.005
    overlap_max = 0.001
    texel_density_variance_max = 1.03
    island_count_max = 80          # safety cap, not a target
    fallback_used_allowed = False
```

Remove or demote as hard failures:

- `convexity_mean`
- `convexity_p10`
- `boundary_smoothness`
- `tendril_count`

Keep them in reports if useful, but do not let them force extra islands unless
they cause distortion/overlap/packing failure.

Packing:

- keep as quality/report initially,
- do not split charts to chase packing,
- use Blender CONCAVE packer for final packing.

## 8. Acceptance Tests

Add tests for the user's three rules.

### Test 1 — Mandatory 90-Degree Seam

Fixture: two planes joined at 90 degrees.

Assert:

- shared edge is in final seam set,
- missing mandatory seams count is zero.

### Test 2 — No Split When Distortion Passes

Fixture: flat plane or simple low-curvature surface.

Assert:

- island count remains minimal,
- no unnecessary split history.

### Test 3 — Split When Distortion Fails

Fixture: curved/cylindrical or high-curvature patch.

Assert:

- first unwrap exceeds distortion threshold,
- refinement splits worst island,
- final distortion decreases,
- island count increases for recorded reason `checker_distortion`.

### Test 4 — Do Not Split For Convexity Alone

Fixture: an oddly shaped but low-distortion disk.

Assert:

- gate passes if distortion/overlap are OK,
- convexity is reported but does not force a split.

### Test 5 — Regression On `humanstatue`

Run `out/generic_run` equivalent:

- target 5850,
- engine chart,
- confirm:
  - mandatory 90 missing = 0,
  - distortion passes,
  - checker renders acceptable,
  - island count is not inflated by artist segmentation.

## 9. What To Do With `artist_uv_agent`

Do not delete it.

For now:

- keep it as experimental `--uv-engine artist`,
- do not make it default,
- do not use it for the latest acceptance criteria,
- do not continue branch/tine work unless the user returns to semantic artist UV.

This avoids losing useful code while keeping the product path clear.

## 10. First Patch Checklist

1. Add final mandatory seam audit to `chart_uv_agent`.
2. Change/demote hard gate items in `chart_uv_agent/gate.py`.
3. Simplify `run_chart_uv` refinement:
   - overlap repair,
   - stretch/checker split,
   - stop.
4. Make split history reason explicit.
5. Ensure `worker/run_quad_retopo_job.py` uses `chart` for the acceptance run.
6. Add tests for the three user rules.
7. Regenerate `out/minimal_distortion/t5850/`.

## 11. Expected Result

The final UV may not look like a hand-authored semantic layout. That is OK for
this track.

It should instead be defensible like this:

```text
The engine produced the fewest islands it could while respecting 90-degree hard
seams and keeping checker distortion below the configured threshold. It only
added islands when measured distortion or overlap required it.
```

That statement matches the user's latest criteria.
