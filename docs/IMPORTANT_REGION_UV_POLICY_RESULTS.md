# Important Region UV Policy — implementation + honest results

Companion to `IMPORTANT_REGION_UV_POLICY_PLAN.ko.md`. Implements the 1st-milestone policy:
an **optional** Important Region Policy layered on `chart_uv_agent.pipeline.run_chart_uv`
(NOT a new engine, NOT a base-solver rewrite). Precedence is **mandatory 90° > region
protection > distortion split**.

## What shipped

- `artist_uv_agent/region_policy.py` — `ImportantRegion`, `RegionPolicyConfig`, `RegionPolicy`,
  `detect_face_front` (axis-required heuristic — never guesses an axis, §11.7), the region-spec
  loader (`load_region_policy`, explicit `face_ids`/`protected_edges` win over heuristic, §5.6),
  the seam-policy constraint bridge, and `build_region_report` (§5.5).
- `chart_uv_agent/pipeline.py` — `run_chart_uv(..., region_policy=None)`. When set, a
  distortion split whose new seam edges would cut a **protected smooth (<90°)** edge is
  **rejected before commit** (§5.4 post-split reject — `split_chart` is untouched), the island
  is left alone, and the reject is recorded. Mandatory ≥90° folds are never protected, so they
  always ship. Also added a thrash guard: once the only over-threshold island is one we can no
  longer split (protected/reverted) and the best *available* island is already under the bar,
  the distortion loop stops honestly instead of splitting fine islands (also fixes a latent
  post-revert thrash; **no-op on the default path**).
- `artist_uv_agent/seam_report.py` — adds a `regions` block to `seam_report.json` (§5.5).
- `worker/run_quad_retopo_job.py` — `--region-spec <path>` for the chart P5 path; emits
  `region_report.json` + `seam_report.regions`. No flag → identical baseline behaviour.
- `tests/test_region_policy.py` — 12 pure tests (§10.1): heuristic detection, axis-required,
  mandatory-stays-mandatory-in-region, protected-smooth-is-forbidden, post-split-reject
  decision, report mandatory-vs-smooth split, explicit-wins, disabled=baseline.
- `.context/region_specs/humanstatue_face_front.json` — the `front_axis="-Y"`, `up_axis="+Z"`
  human-statue spec from the plan's run command.

## Blender run (human statue, t5850, `adaptive_t5850.blend`)

Baseline = no `--region-spec`; Policy = `--region-spec humanstatue_face_front.json`.

| metric | baseline | policy (face_front) |
|---|---|---|
| **mandatory_90_missing** | 0 | **0** |
| **mandatory_90_uv_unsplit** | 0 | **0** |
| **raster_overlap_ratio** | 0.0018 | **0.00055** (≤ 0.005) |
| **uv_bounds_ok** | true | **true** |
| **fallback_used** | false | **false** |
| global stretch | 0.158 | 0.188 (+0.030, ≤ +0.05 budget) |
| island count | 31 | 58 (**+27, over the +10 budget**) |
| worst-island distortion | 0.345 | 3.074 (the protected face island) |
| gate verdict | accepted | **failed** (`worst_island_distortion`) |
| face_front faces / protected edges | — | 331 / 349 (worker) |
| rejected protected splits | — | 1 (recorded with cut edge ids) |
| face_front mandatory / smooth seams | — | 3 / 25 (reported separately) |

**All five §9.1 hard-correctness gates hold under the policy.** The `regions` block reports
mandatory-vs-smooth seams and the rejected split, exactly as §5.5 requires.

## Honest finding (§11.3): post-split reject alone does NOT improve the face on this asset

Apples-to-apples face smooth-seam count (same heuristic region applied to both shipped seam
sets): **baseline 67 smooth → policy 71 smooth (+4, worse)**, with island count +27.

Root cause, from the run history: the human-statue face, left whole by the rejected
distortion split, is **self-overlapping** when unwrapped. The overlap-correctness pass is a
**hard gate** that must run and is **region-blind** — so it re-cuts the protected face anyway,
more messily than the clean distortion split would have. The clean distortion split that
baseline accepted *also* resolved the overlap; rejecting it trades one tidy seam for several
overlap-driven ones plus residual distortion. Most of the face's 67 baseline smooth seams come
from the **initial segmentation**, which this milestone explicitly does not touch (§11.6).

So on this asset the mechanism is net-neutral-to-slightly-negative. This is the honest
best-effort result the plan anticipates (§13: "the face won't be a perfect production UV after
this"). The implementation is correct and faithful to the milestone; the *outcome goal*
(fewer face smooth seams) needs the **region-aware segmentation / cut reroute** the plan
deferred past this milestone (§5.4 "do not reroute the split path in v1"). Reducing face smooth
seams requires the segmentation and overlap-correctness passes to become region-aware, not just
post-split reject.

## Safety / optionality (§11.5)

The policy is opt-in. Without `--region-spec`, the chart engine is byte-for-byte the baseline
(verified: 31 islands / stretch 0.1577 / gate accepted, identical with and without the new
guard). Hard gates are never loosened to force `accepted` (§11.4) — the policy run is reported
honestly as `failed (worst_island_distortion)`, best-effort.

## Known curiosity

`detect_face_front` resolves to 331 faces inside the live worker job but 486 faces when run
standalone on the same blend/mesh/spec (deterministic in each context, nonfinite=0 both ways).
It does not change the conclusion above (overlap-correctness re-cuts the face regardless of
region size), but it is worth tracing before relying on the absolute face-region size.

## Recommended next step

Region-aware **segmentation cost** + overlap-correctness **cut reroute** (route required cuts
toward the head-back / neck-under preferred zones instead of across the face front), so the
hard-gate overlap pass stops shattering the protected region. That is the deferred §5.4 work
and is what will actually move the face smooth-seam count down.
