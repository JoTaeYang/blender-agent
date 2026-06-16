# Region-Aware Face UV Recovery — implementation + results

Companion to `REGION_AWARE_FACE_UV_RECOVERY_PLAN.ko.md`. Pivots away from the failed v1
post-split reject (`IMPORTANT_REGION_UV_POLICY_*`) to a **front-stage**, region-aware approach.
No new engine, no base-solver rewrite, mandatory ≥90° seams always preserved, opt-in.

## What shipped

- `artist_uv_agent/region_policy.py` — 3-zone model (`face_front_core` / `face_side_transition`
  / `head_back_neck_preferred`, DISJOINT face sets — never one giant protected island).
  `classify_face_regions`, `region_edge_cost_multiplier`, `region_protected_merge`,
  `region_boundary_audit`; `RegionPolicy.mode` (`face_recovery` default / `post_split_reject`
  experimental) + `edge_cost_multiplier`; v2 spec loader.
- `chart_uv_agent/segmentation.py` — `edge_cut_cost(..., region_policy=)`: a sub-fold cut on a
  face_front_core edge gets ×(core cost), a head_back/neck edge ×0.25; a ≥90° fold is **never**
  multiplied (mandatory wins, §6.2). Threaded through `_cut_path_to_boundary` /
  `split_welded_folds` (repair reroute, §6.4).
- `chart_uv_agent/pipeline.py` — `run_chart_uv` runs `region_protected_merge` after `segment()`
  in `face_recovery` mode (Option C), threads region cost into the welded-fold repair (Option
  B), and **gates the old post-split reject behind `mode=="post_split_reject"` (off by default,
  §2.1)**. Region audit/merge surfaced in the result + `seam_report.regions`.
- `worker/run_quad_retopo_job.py` — `region_report.json` now carries `mode` / `protected_merges`
  / per-zone audit. `.context/region_specs/humanstatue_face_recovery.json` (v2, 3 zones).
- Tests: `tests/test_region_policy.py` (20, incl. 3-zone classify, region edge cost
  up/down/mandatory-unchanged, protected merge removable/mandatory-blocked/non-disk-rejected,
  v2 loader + default mode). Full suite green (500).

## Blender run (human statue, t5850) vs the base chart baseline

| metric | baseline | **face_recovery** | v1 reject (failed) |
|---|---|---|---|
| mandatory_90_missing / uv_unsplit | 0 / 0 | **0 / 0** | 0 / 0 |
| raster_overlap (≤0.005) | 0.0018 | **0.0021** | 0.00055 |
| uv_bounds_ok / fallback | ok / no | **ok / no** | ok / no |
| gate verdict | accepted | **accepted** | failed |
| island count | 31 | **35 (+4)** | 58 (+27) |
| global stretch | 0.158 | **0.171 (+0.013)** | 0.188 |
| worst-island distortion | 0.345 | **0.349** | 3.074 |
| **core-front smooth seams** | 43 | **39 (−4)** | — |
| **face(core+side) smooth seams** | 282 | **276 (−6)** | 71 (+4 worse) |

Apples-to-apples (same heuristic region applied to both shipped seam sets). **Every §8.4
regression budget holds** (islands ≤ +10, stretch ≤ +0.05, worst ≤ 0.75), all §9.1 hard gates
hold, the gate stays **accepted**, and face smooth seams **decrease** (§8.3 direction met). None
of the §10 failure conditions trigger. This fully reverses the v1 catastrophe (gate-fail,
+27 islands, worst 3.074).

## Honest finding: the gain is from the reroute (B); the merge (C) is correctly inert here

`protected_merges = 0` on the statue. Diagnosing the segmentation: the face_front_core spans 10
charts, but **all 3 core-touching chart boundaries carry a mandatory ≥90° fold** (the brow,
nose bridge and jaw are genuine anatomical creases) and their merged unions are ~158–176° normal
cones — a near-full-shell. So the protected merge correctly refuses every one (mandatory wins +
the cone guard prevents the v1 distortion blow-up). There is simply **no safe merge** on this
face: most face-front seams ARE mandatory folds the artist wants, and the few removable smooth
seams were trimmed by the region-aware repair reroute (core 43→39).

This is the right behaviour, not a miss: the merge is the safety-bounded lever, and on an asset
whose face is bounded by real ≥90° creases it stays its hand. On an asset whose face front is
fragmented by *smooth* segmentation seams (no fold between the pieces, union cone < 68°) the
merge WILL fire and coalesce them — the unit tests cover that path.

## Safety / optionality

Opt-in. Without `--region-spec` the chart engine is byte-for-byte the baseline (31 islands /
0.1577 / accepted, verified). The post-split reject path is retained only for `mode =
"post_split_reject"` A/B comparison and is never the default (§2.1). No threshold was relaxed to
keep the gate green (§10) — it is genuinely accepted.

## Recommended next step

To move the face-front smooth-seam count down further on creased faces, the lever is
region-aware **segmentation split placement** (Option A done right — bias `split_chart`'s seed
cut toward the head-back/neck preferred zone) rather than post-hoc merge, plus a signed-dihedral
cut cost so a concave neck crease is preferred over a convex cheek crease. The current milestone
delivers the safe, no-regression baseline that makes that next step measurable.
