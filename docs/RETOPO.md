# AI Retopology Agent — Phases 1–6

This is a sibling engine to the UV Layout Agent (`uv_agent`), implementing the
retopology plan in `.context/ai_retopology_agent_plan_eng.md`. It reuses the same
mesh representation (`uv_agent.geometry.mesh_graph.MeshGraph`).

**Phase 1 goal** (plan §10): turn a selected high-poly object into a separate
low-poly object near a target face count, project it back onto the high-poly
surface, and create a result object.

**Phase 2 goal** (plan §10): numerically validate the generated topology — face
count vs target, quad/triangle/n-gon counts, quad ratio, non-manifold geometry —
and emit an accepted/retry/failed verdict as JSON.

**Phase 3 goal** (plan §10): evaluate how well the low-poly preserves the
high-poly's shape — surface distance and normal deviation — so a result is judged
by more than reduced polygon count.

**Phase 4 goal** (plan §10): let the user pick a topology *level* (high/mid/low/
custom), batch-generate multiple LOD versions from one high-poly, and compare them.

**Phase 5 goal** (plan §10): preserve the edges/regions that carry shape — detect
hard edges & curvature, keep density on features/silhouettes, reduce it in flat
areas — while still cutting polycount.

**Phase 6 goal** (plan §10): make the mesh flow more natural — a quad-flow score,
valence detection, triangle→quad cleanup, and local relax.

**Completion criteria**: a 100k-face model is convertible to ~50k/~10k faces
(Phase 1); the result is numerically confirmed for "no n-gons", quad ratio, and
closeness to target (Phase 2); it is scored for shape preservation (Phase 3);
50k/20k/10k versions are generated and compared from one object (Phase 4);
important features/silhouettes are preserved while reducing polycount (Phase 5);
and the result has more natural quad flow than a plain decimate (Phase 6). All
verified on a real 9.8M-face model — see below.

## Layout

```
retopo_agent/
  geometry/decimate.py       # Blender-free vertex-clustering decimation + target search
  geometry/target_search.py  # voxel-size / QuadriFlow target-count control loops (§15.7)
  geometry/validate.py       # Phase 2 topology validator (§6.6, §15.6)
  geometry/shape_eval.py     # Phase 3 shape evaluator (§6.7, §15.6) — brute-force nearest-surface
  geometry/features.py       # Phase 5 feature detection (hard edges, curvature, material seams) (§6.1, §6.3)
  geometry/quadflow.py       # Phase 6 quad-flow score + tris->quads + Taubin relax (§6.6, §6.8)
  geometry/feature_compare.py # Decimation Phase D3 feature preserve off/on comparison (decimation §7)
  geometry/normals.py        # Decimation Phase D4 Auto-Smooth/Weighted-Normal model + deviation metric
  blender/cleanup.py         # Decimation Phase D4 Blender normal cleanup (auto smooth, weighted, transfer)
  levels.py                  # Phase 4 topology-level presets + LOD comparison types (§6.2)
  pipeline.py                # Phase 4 offline LOD batch (decimate -> validate -> shape)
  io/fixtures.py             # synthetic high-poly meshes (UV sphere, subdivided cube)
  blender/retopo.py          # Blender adapter: QuadriFlow -> voxel -> cluster-decimate, + shrinkwrap
  blender/decimate.py        # Decimation Optimize: Decimate (Collapse) + ratio search + feature vgroup (D1, D3)
  blender/shape.py           # Blender BVH shape evaluation + silhouette preview render
  blender/batch.py           # Phase 4 Blender LOD batch generation + comparison
  blender/features.py        # Phase 5 mark-sharp-by-angle + scalable feature analysis
  blender/quadflow.py        # Phase 6 tris->quads + relax + re-projection
worker/run_retopo_job.py     # headless Blender worker entrypoint (--provider mock)
tests/test_retopo_decimate.py
tests/test_retopo_target_search.py
tests/test_retopo_validate.py
tests/test_retopo_shape_eval.py
tests/test_retopo_levels.py
tests/test_retopo_features.py
tests/test_retopo_quadflow.py
tests/test_retopo_decimate_collapse.py   # Decimation Optimize mode (Phases D1-D3)
tests/test_retopo_normals.py             # Decimation Phase D4 Auto-Smooth/Weighted-Normal model
```

## Generation pipeline (plan §15.5) with target-count control (§15.7)

Inside Blender, `generate_lowpoly_object` follows the spec's fallback ladder, and
crucially **closes the loop on the target face count** — it measures the actual
result after every attempt and retries the control parameter until the result
lands in the acceptance band (this is what fixes `target 10000 -> actual 2774`):

1. **QuadriFlow Remesh** — default, quad-oriented. `target_faces` is only a hint,
   so the request is retried, scaled by `target/actual`, until it converges.
   Skipped for very large inputs (`> 1.5M` faces), where it is impractically slow
   and tends to fail silently.
2. **Voxel Remesh** — fallback for messy/huge inputs. Voxel *size* is
   **binary-searched** (face count ≈ area / voxel², so `actual < target` ⇒ shrink
   the voxel) until the face count is on target.
3. **Cluster decimate** — deterministic Blender-free last resort
   (`retopo_agent.geometry.decimate`), guaranteeing a result everywhere.

The control loops live in `retopo_agent.geometry.target_search` (pure, unit-tested
offline) and report a quality band per §15.6: `accepted` (error ≤ 0.15),
`retry` (≤ 0.30), `failed` (> 0.30). Each remesh attempt restarts from a fresh
copy of the high-poly, since remeshes are destructive.

It then applies a **Shrinkwrap** modifier targeting the original high-poly
(plan §6.5) and emits a result object named `{name}_LOW_{target}` in the
`AI_Retopo_Results` collection.

### Verified on the anchor model

`sample/anchor.obj` (**9,828,143 faces**) → target 10,000:

| | method | actual faces | error | band |
| --- | --- | --- | --- | --- |
| before (single-shot voxel) | voxel_remesh | 2,774 | 72.3% | failed |
| after (voxel-size search) | voxel_remesh | 8,874 | 11.3% | **accepted** |

QuadriFlow auto-skipped (input > 1.5M faces); voxel search converged in 2
iterations (~12s). The worker / run script removes the high-poly source before
saving, so `lowpoly.blend` holds only the low-poly result (~390 KB, not 778 MB).

The cluster-decimate core is also the path used by unit tests and offline
`--provider mock` runs (plan §15.12), so the whole reduction is testable without
Blender. It overlays a cubic grid, collapses each cell's vertices to their
average, drops degenerate/duplicate faces, and binary-searches the grid
resolution so the output face count lands near the target. Output face count is
monotonic in grid resolution, so the search converges in a few iterations.

> The offline cluster-decimate path reduces polygon count and preserves the
> silhouette; it does **not** guarantee quad-clean topology (that is QuadriFlow's
> job in Blender, and a later offline phase).

## Phase 2 — topology validator (plan §6.6, §15.6)

`retopo_agent.geometry.validate.validate_topology(mesh, target_face_count, ...)`
runs on a `MeshGraph` (synthetic or Blender-extracted) and reports, then gates,
the §6.6 metrics:

| metric | accepted | retry | failed |
| --- | --- | --- | --- |
| `target_error_ratio` | ≤ 0.15 | ≤ 0.30 | > 0.30 |
| `quad_ratio` (if `quad_required`) | ≥ 0.98 | ≥ 0.90 | < 0.90 |
| `triangle_ratio` | ≤ 0.02 | ≤ 0.10 | > 0.10 |
| `ngon_count` (if not `ngon_allowed`) | 0 | > 0 (cleanup) | > 0 after cleanup |
| `non_manifold_edge_count` | 0 | > 0 (repair) | — |
| `open_boundary_count` (if `expect_closed`) | 0 | > 0 | — |

The overall `status` is the worst band, and `reasons[]` records which metric
drove it (so the Phase 7/8 repair loop can target it, §15.7). `valence_issue_count`
is reported as an informational quad-flow signal but not gated. `edge_flow_score`
is deferred to Phase 6. The worker writes `validation_report.json` after
generation (plan §8.2 step 7).

Per §10 "treat n-gon discovery as failure": an n-gon means *not accepted* — it
drops to `retry` and triggers repair; the literal `failed` band is reserved for
n-gons that survive a cleanup pass (`ngon_after_cleanup=True`).

### Verified on the anchor low-poly

Validating the saved anchor result (8,874 faces) → **`status: accepted`**:
`quad_ratio 1.0` (Blender 5's voxel remesh is quad-dominant), `triangle_count 0`,
`ngon_count 0`, `non_manifold_edge_count 0`, `open_boundary_count 0`,
`target_error_ratio 0.1126`. `valence_issue_count 173` (informational). So Phase 2
numerically confirms the Phase 1 output is usable.
→ `.context/runs/anchor_phase1_v2/validation_report.json`.

## Phase 3 — shape-preservation evaluator (plan §6.7, §15.6)

`retopo_agent.geometry.shape_eval.evaluate_shape_match(high, low, ...)` measures
how closely the low-poly follows the high-poly surface, normalizing distances by
the high-poly bounding-box diagonal (`surface_distance_ratio = distance / diag`):

| metric | accepted | retry | failed |
| --- | --- | --- | --- |
| `surface_distance_mean_ratio` | ≤ 0.01 | ≤ 0.03 | > 0.03 |
| `surface_distance_max_ratio` | ≤ 0.05 | ≤ 0.10 | > 0.10 |
| `normal_deviation_mean_deg` | ≤ 12 | ≤ 25 | > 25 |

Distances come from points sampled on the low-poly (vertices + face centroids) to
the nearest point on the high-poly surface. Normal deviation is the mean angle
between low and nearest-high face normals, **folded into [0, 90]** so a flipped
winding isn't mistaken for a 180° error. `volume_error_ratio` is reported but not
gated; `silhouette_error` / `curvature_preservation_score` (§6.7) are deferred.

The pure module brute-forces nearest-surface (vectorized point-to-triangle) for
tests; the Blender adapter `retopo_agent.blender.shape` does the same with a
`mathutils` BVH tree (`find_nearest`) so it scales to multi-million-face inputs,
and also renders a best-effort silhouette `preview.png` (Workbench engine). The
worker writes `shape_report.json` after validation, while both meshes still exist
(plan §8.2 step 8).

### Verified end-to-end on the anchor (9.8M → 8,874 faces)

Full Phase 1→2→3 pipeline in Blender; shape eval over 16,874 samples in 5.8s via
BVH → **`status: accepted`**:

| metric | value | band |
| --- | --- | --- |
| `surface_distance_mean_ratio` | 0.00013 | accepted |
| `surface_distance_max_ratio` | 0.00288 | accepted |
| `normal_deviation_mean_deg` | 6.41 | accepted |
| `volume_error_ratio` | 0.035 | (informational) |

The shrinkwrap projection keeps the low-poly within 0.013% (mean) / 0.29% (max) of
the original surface. Outputs (incl. rendered `preview.png`) →
`.context/runs/anchor_phase3/`.

## Phase 4 — topology level control + LOD comparison (plan §6.2)

`retopo_agent.levels` defines the presets — `high_retopo` (0.5×), `mid_retopo`
(0.2×), `low_retopo` (0.1× of the source), and `custom` (absolute target) — so a
100k input resolves to 50k / 20k / 10k. `plan_topology_levels` turns a mix of
levels and explicit targets into a de-duplicated, high-detail-first list of LOD
plans.

Batch generation runs one LOD per plan and gathers each LOD's headline metrics
(face count, target error, validation status, quad ratio, shape status/ratios)
into a `LodComparison` → `comparison.json`:

- **offline** (`retopo_agent.pipeline.generate_lod_set_offline`) — decimate →
  validate → shape per LOD; the deterministic `--provider mock` / test path;
- **Blender** (`retopo_agent.blender.batch.generate_and_evaluate_lods`) — the
  production path (QuadriFlow/voxel + BVH shape), invoked by the worker when
  `--levels` or `--targets` is given. All LOD objects are kept in the scene.

### Verified on the anchor — 50k / 20k / 10k from one object

Three LODs batch-generated from the 9.8M-face anchor in 49.7s, **all accepted**:

| target | actual | error | validation | shape | quad_ratio | normal dev |
| --- | --- | --- | --- | --- | --- | --- |
| 50,000 | 46,862 | 6.3% | accepted | accepted | 1.0 | 2.78° |
| 20,000 | 18,568 | 7.2% | accepted | accepted | 1.0 | 4.38° |
| 10,000 | 8,874 | 11.3% | accepted | accepted | 1.0 | 6.41° |

Face count decreases monotonically and shape fidelity degrades gracefully with
detail (normal deviation 2.8° → 6.4°). Outputs → `.context/runs/anchor_phase4/`
(`comparison.json`, `lowpoly_lods.blend`).

## Phase 5 — feature-aware retopology (plan §6.1, §6.3)

`retopo_agent.geometry.features` finds the shape-defining features on a
`MeshGraph` from its dihedral angles: `detect_hard_edges` (crease/boundary edges),
`vertex_feature_scores` / `feature_vertex_mask` (per-vertex curvature),
`material_boundary_edges`, `analyze_features` (a `FeatureReport`), and
`plan_feature_preservation` (§6.3 schema of regions to protect).

Two ways those features steer generation:

- **Offline** — `feature_aware_decimate` / `feature_aware_decimate_to_target` keep
  feature vertices *exactly* while collapsing flat regions onto the grid, so hard
  edges and silhouettes stay crisp. Flat clustering also keys on a **quantized
  vertex normal**, so vertices on differently-facing surfaces (e.g. opposite
  sides of a thin shell) never average into an off-surface point.
- **Blender** — `blender/features.py` marks edges above an angle sharp so
  QuadriFlow's `use_preserve_sharp` keeps panel lines; the voxel path uses
  `voxel_adaptivity` to thin flat areas while keeping density on curvature.
  Enabled via `--preserve-features` / `--feature-angle` / `--voxel-adaptivity`;
  a sampled `feature_report.json` is always written (scales to 19.6M edges).

### Verified

- **Offline (decisive):** for a subdivided cube reduced to ~150 faces,
  feature-aware decimation keeps the box silhouette far tighter than uniform
  clustering — `surface_distance_max_ratio` drops from **0.19 → 0.038** (uniform
  rounds the corners; feature-aware preserves all 8).
- **Blender, anchor:** feature analysis over **19.6M edges in 3.1s**
  (hard-edge ratio 0.32%); adaptive-voxel generation (`adaptivity=0.5`) →
  10,149 faces, **accepted** (shape max ratio 0.0046, normal dev 8.85°), landing
  within 1.5% of target. QuadriFlow + sharp marking keeps all hard edges on a
  hard-surface test mesh. → `.context/runs/phase5/`.

## Phase 6 — quad-flow improvement (plan §6.6, §6.8)

`retopo_agent.geometry.quadflow.quad_flow_score` is the §6.6 `edge_flow_score`: a
0..1 blend of **quad fraction**, **valence regularity** (closeness of interior
vertices to valence 4), and **face squareness** (quad corners near 90°). It also
returns a valence histogram and issue count.

Improvement ops (plan §6.8):

- `tris_to_quads` — greedily merge adjacent triangle pairs into quads, best
  (near-coplanar + square) first, never across hard edges;
- `relax_vertices` — Taubin (λ\|μ) smoothing, which relaxes edge flow without the
  shrinkage of plain Laplacian; feature and boundary vertices are pinned;
- `improve_quad_flow` chains them. The Blender adapter `blender/quadflow.py` does
  the equivalent with `tris_convert_to_quads` + `vertices_smooth` and re-projects
  onto the high-poly. Enabled in the worker with `--improve-quad-flow`, which
  writes `quadflow_report.json` (before/after).

### Verified — more natural flow than a plain decimate

| input | quad-flow score before → after | quad fraction |
| --- | --- | --- |
| icosphere (20k triangles) | **0.150 → 0.932** | 0.0 → 0.985 |
| anchor, plain Decimate-collapse (8.6k tris) | **0.381 → 0.463** | 0.16 → 0.26 |

The icosphere shows the dramatic case; on the messy anchor decimate (valences up
to 82) the improvement is real and the re-projected result stays shape-**accepted**
(max ratio 0.018, normal dev 9.95°). → `.context/runs/phase6/`.

## Decimation Optimize mode — Phase D1 (decimation plan §7)

A **sibling mode** to quad retopo, selected with `--mode decimation_optimize`
(default stays `quad_retopo`). Its goal is the opposite tradeoff — ZBrush
Decimation-Master-style aggressive polygon reduction that preserves the
silhouette, **triangles allowed, n-gons forbidden** — not clean quad edge flow.
It is a separate branch from Phases 1–6, not a replacement (plan §10).

```bash
blender --background input.blend \
  --python worker/run_retopo_job.py -- \
  --provider mock \
  --mode decimation_optimize \
  --object-name HighPolyObject \
  --target-face-count 2000
```

Phase D1 pipeline (`_run_decimation` in the worker → `blender/decimate.py`):

```text
duplicate -> Decimate (COLLAPSE) modifier -> ratio search -> result object
```

The Decimate modifier's `ratio` only loosely predicts the final face count, so —
exactly like the QuadriFlow / voxel paths — the generator **closes the loop on the
target**: it measures the actual result after each attempt and rescales the ratio
by `target / actual` until the band is `accepted`. The control loop
`retopo_agent.geometry.target_search.search_decimate_ratio` is pure and unit-tested
offline (the modifier modelled as `faces ≈ source * ratio`); each attempt restarts
from a fresh copy of the high-poly because applying a modifier is destructive. The
result object is named `{name}_DECIMATED_{target}` in `AI_Retopo_Results`.

Outputs (plan §8): `decimation_plan.json` (the §9 plan schema, with
`"mode": "decimation_optimize"`), `generation_report.json` (method, ratio, source/
target/actual face counts, `target_error_ratio`, band, feature-preservation info,
plus the DM1 plateau metadata below), `decimation_diagnosis.json` (DM2 pre-process,
below), `component_budget.json` (DM3 component budget, below),
`importance_map.json` (DM4 importance map, below),
`decimation_attempts.json` (DM5 retry ladder, below — when the primary collapse
misses target), `feature_report.json` + `shape_report.json` (Phases D3/D2, below), an
optional `feature_comparison.json` (Phase D3 `--compare-features`), and the exported
`lowpoly.blend` / `lowpoly.fbx`. Normal cleanup (D4) is a later phase.

> Phase D1 completion criterion (plan §7): reducing `sample/anchor.obj` (9,828,864
> faces) to a target of 2,000 lands in the **accepted** band. The ratio search
> reaches it on the first guess (`ratio = 2000 / 9_828_864`) for a linear
> collapse — verified offline in `tests/test_retopo_decimate_collapse.py`.

### Phase DM1 — plateau detection (decimation master plan §4)

A real Collapse modifier does **not** behave linearly on `anchor.obj`: it floors at
8008 faces no matter how small the ratio (non-manifold / detached geometry hits a
topology floor). `search_decimate_ratio` detects this — when the ratio falls but the
face count holds within `plateau_tol` for `plateau_repeats` consecutive
measurements, it stops early and records `stopped_reason="decimate_collapse_plateau"`
with `plateau_face_count` / `plateau_ratio`. A `min_ratio` clamp is tracked
separately (`hit_min_ratio`) so it is not mistaken for a plateau. `generation_report.json`
carries `stopped_reason`, `plateau_face_count`, `plateau_ratio`, `hit_min_ratio`,
`search_iterations`, and `search_history`, so a `failed` band is explained rather
than left bare (verified in `tests/test_retopo_decimate_collapse.py`).

### Phase DM2 — pre-process topology diagnosis (decimation master plan §5)

`diagnose_topology` (pure, `retopo_agent/geometry/diagnosis.py`) inspects the mesh
for the risk factors and constraints ZBrush's pre-process pass would: connected
components, tiny detached shells, open boundary / non-manifold edges, degenerate
faces, duplicate / near-duplicate vertices, duplicate faces, very small triangles,
face-area distribution, and material / UV-seam / sharp-normal boundaries. It runs on
a `MeshGraph`, so it is unit-tested offline (`tests/test_retopo_diagnosis.py`) and
wrapped by `retopo_agent/blender/diagnosis.py` for Blender meshes. The worker
diagnoses the **decimated result** — e.g. the anchor's 8008-face plateau, where the
25-component / 20-tiny structure that blocks a lower target shows up — and writes
`decimation_diagnosis.json` (the plan §5 contract: `component_count`,
`largest_component_face_ratio`, `boundary_edge_count`, `non_manifold_edge_count`,
`tiny_component_count`, `recommended_policy`, plus extended diagnostics). The
`recommended_policy` (`preserve_all` / `component_budget` / `largest_only`, via
`recommend_component_policy`) is the input the DM3 / DM5 retry ladder uses to choose
a component-handling strategy, and is also surfaced in `generation_report.json`.

### Phase DM3 — component budget policy (decimation master plan §6)

At very low targets the small detached shells of a multi-component mesh (the
anchor's 25 shells, 20 tiny) eat a disproportionate face budget. `plan_component_budget`
(pure, `retopo_agent/geometry/component_budget.py`) measures each connected component
(face count, surface area, bbox, materials), scores its importance (area / face-count
/ size × material weight), and distributes the target face budget across components
under one of three policies:

| `--component-policy` | non-dominant shells | tiny shells |
| --- | --- | --- |
| `preserve_all` | importance-weighted share | importance-weighted share |
| `budget` (`component_budget`) | importance-weighted share | minimal shell, or removed under `allow_removal` |
| `largest_only` | minimal shell, or removed under `allow_removal` | minimal shell, or removed under `allow_removal` |

Each active component's budget is clamped to `[min(face_count, min_shell), face_count]`
(decimation only reduces), and at least one component is always kept active so a policy
never empties the mesh. Tiny-component **removal is off by default**; it is enabled by
`--allow-component-removal` (auto-on under `--decimation-policy strict_target`). The
worker resolves the policy from `--component-policy` if given, else the DM2 diagnosis
recommendation, and writes `component_budget.json`: per-component measurements +
planned action (`decimate` / `min_shell` / `remove`), the allocated budget, the
removed object/face counts, and the achievable lower-bound face count **with vs
without** tiny-component removal (the plan §6 comparison). Executing the per-component
collapse in Blender is the DM5 retry's job — DM3 produces the plan it consumes
(unit-tested in `tests/test_retopo_component_budget.py`).

### Phase DM4 — importance map (decimation master plan §7)

ZBrush-style decimation does not reduce every region by the same ratio: features
stay dense, flat areas collapse hard. `compute_importance_map` (pure,
`retopo_agent/geometry/importance.py`) turns a `MeshGraph` into a continuous
**importance map** — a value in `[0, 1]` per vertex, edge and face — combining the
plan §7 sources: graded curvature + the hard-edge threshold, open boundary,
non-manifold boundary, material boundary, UV seam, sharp-normal boundary, the
face-area percentile (small faces = fine detail), and an optional user vertex
group. Sources are combined by a **weighted soft-OR** (max of the weighted
contributions), so importance means "the strongest reason to keep this element" and
stays in `[0, 1]`; edges carry the feature signals, vertices take the max over their
incident edges (plus area / user weight), and faces the max over their vertices.

The worker writes `importance_map.json` (the plan §7 contract: `importance_stats`
min/mean/max + the `sources` that actually fired, plus edge/face stats). The map
feeds the Decimate Collapse two ways (plan §7):

- **short term** — `importance_to_vertex_weights` maps importance to vertex-group
  weights and `--use-importance-map` drives the collapse with the graded map
  (curvature / seams / material borders protected proportionally, not just hard
  edges); `--preserve-features-strength` sets the modifier's `vertex_group_factor`,
  the global protection strength. On a mesh too large for a full Python map the
  collapse falls back to the binary hard-edge / boundary feature group, still
  strength-controlled.
- **mid term** — the same map becomes the importance penalty in the DM6 custom QEM
  edge-collapse cost.

Unit-tested per source in `tests/test_retopo_importance.py`.

### Phase DM5 — progressive decimation retry ladder (decimation master plan §8)

When the primary Collapse pass misses the target band (typically a DM1 plateau),
the worker does **not** jump to a voxel/cluster remesh — it escalates through more
aggressive strategies within the triangle-decimation family, on the small plateau
result (e.g. the anchor's 8008 faces):

| attempt | method | strategy |
| --- | --- | --- |
| 1 | `collapse_full_feature_protection` | feature-aware collapse, protect ≥ `feature_angle` |
| 2 | `collapse_relaxed_feature_protection` | feature-aware collapse, relaxed angle (fewer protected verts) |
| 3 | `cleanup_then_collapse` | weld near-duplicate verts + drop degenerate/duplicate faces, then collapse |
| 4 | `planar_flat_region_reduce_then_collapse` | flat-region reduction, no feature protection |
| 5 | `component_budget_then_collapse` | DM3 tiny-component removal (under `allow_removal`), then collapse |
| 6 | `custom_qem_triangle_collapse` | DM6 — **skipped until implemented** |

`run_retry_ladder` (pure, `retopo_agent/geometry/retry_ladder.py`) is the driver;
the strategies are pure-geometry transforms on a `MeshGraph` reusing the existing
decimator, weld/cleanup, and DM3 component analysis, each scored with
`evaluate_shape_match`. The DM5/DM7 policy: keep escalating while the shape stays
acceptable, stop on `target accepted` (success / warning success), and **roll back
to the last shape-accepted attempt the moment one breaks the shape**. Every attempt
records why the target was (not) met. The worker (`retopo_agent/blender/retry_ladder.py`)
runs it on the plateau result, scores shape against the high-poly when small enough
to extract (else against the plateau base), writes `decimation_attempts.json`
(`selected_attempt`, `selection_reason`, per-attempt reports), and swaps in the
selected candidate when it improves on the plateau. Orchestration and strategies are
unit-tested in `tests/test_retopo_retry_ladder.py`.

### Phase D2 — shape preservation (decimation plan §6.5)

After generation, `_run_decimation` evaluates how well the triangle LOD keeps the
high-poly's shape, **reusing the Phase 3 evaluator** (`evaluate_shape_match_blender`,
BVH-based) while both meshes still exist, and writes `shape_report.json`. The only
difference from quad retopo is the gating bands: a triangle LOD tolerates more
normal deviation, so the evaluator is parameterized with `ShapeThresholds` and the
decimation path passes `DECIMATION_SHAPE_THRESHOLDS`:

| metric | accepted | retry | failed |
| --- | --- | --- | --- |
| `surface_distance_mean_ratio` | ≤ 0.01 | ≤ 0.03 | > 0.03 |
| `surface_distance_max_ratio` | ≤ 0.05 | ≤ 0.10 | > 0.10 |
| `normal_deviation_mean_deg` | ≤ **20** | ≤ 40 | > 40 |

(`volume_error_ratio` is reported but not gated, as in Phase 3.) The overall
`status` is the worst band; when it is not `accepted`, `reasons[]` names the metric
that drove it and the worker prints it — the Phase D2 completion criterion ("output
the reason for failure/retry when applicable"). The defaults are unchanged, so the
quad-retopo Phase 3 path keeps its ≤ 12° normal cutoff.

### Phase D3 — feature-aware decimation (decimation plan §6, §7)

Flat areas should collapse aggressively while hard edges / high-curvature regions
keep their density (plan §6 "preserve density in important areas, decimate flat
areas"). Two pieces, both reusing the Phase 5 feature toolkit:

- **Blender** — `--preserve-features` makes `generate_decimated_object` put every
  vertex on a hard edge (dihedral ≥ `--feature-angle`, default 30°) or open
  boundary into an `AI_Decimate_Features` vertex group, then drives the Decimate
  (Collapse) modifier's `vertex_group` weighting from it so those regions are
  decimated *less*. The feature indices are computed once from the source and
  re-applied on each ratio-search attempt (a fresh source copy preserves vertex
  ordering). `feature_report.json` (hard-edge ratio, curvature) is always written.
- **Offline / comparison** — `retopo_agent.geometry.feature_compare.compare_feature_preservation`
  decimates to the **same target** twice — plain `decimate_to_target` (off) vs
  `feature_aware_decimate_to_target` (on, keying off `feature_vertex_mask`) — and
  scores both with the decimation shape bands. `--compare-features` runs the same
  off/on pair in Blender and writes `feature_comparison.json`, keeping both result
  objects in the scene (the Phase D3 completion criterion: "compare feature
  preservation off/on results at the same target face count").

The headline metric is `surface_distance_max_ratio_improvement` (off minus on) —
positive means preservation cut the worst-case deviation.

### Verified offline — subdivided cube to 150 faces

`build_subdivided_cube(divisions=12)` (864 faces; the 12 cube edges carry 140
feature vertices) → target 150:

| variant | method | actual faces | surface_distance_max_ratio | shape |
| --- | --- | --- | --- | --- |
| preserve off | cluster_decimate | 150 | 0.038 | accepted |
| preserve on | feature_aware_cluster_decimate | 264 | **0.000** | accepted |

Feature preservation keeps every hard edge exactly, so the box silhouette is
perfect (max deviation 0.038 → 0.0) — at the cost of overshooting the target (the
140 preserved corner/edge vertices form a face-count floor). The tradeoff is
exactly what the off/on comparison is meant to expose.

### Phase D4 — normal / visual cleanup (decimation plan §6.4, §7)

A freshly decimated triangle LOD shades *flat* (each face uses its own normal), so
a curved surface facets visibly and the per-face normal deviates from the smooth
original. Phase D4 reduces that with Auto Smooth + Weighted Normal + optional
normal transfer:

- **Blender** — `--normal-cleanup` runs `retopo_agent.blender.cleanup.cleanup_decimated_normals`
  on the result: optional Triangulate, **Auto Smooth** (faces set smooth + edges
  marked sharp above `--auto-smooth-angle`, reusing Phase 5's
  `mark_sharp_edges_by_angle`, i.e. the smoothing-split control), a **Weighted
  Normal** modifier (`keep_sharp`), and — with `--transfer-normals` — a **Data
  Transfer** modifier copying the high-poly's custom split normals. Each step is
  best-effort and version-tolerant; what was applied is recorded under
  `normal_cleanup` in `generation_report.json`.
- **Offline / metric** — `retopo_agent.geometry.normals` is the Blender-free model:
  `face_shading_normals` splits each vertex into one shading normal per *smoothing
  group* (the fan of faces reachable through edges below the auto-smooth angle;
  sharp creases break the fan), area-weighted for the Weighted Normal case.
  `evaluate_normal_cleanup` compares the high-poly normal against the low-poly's
  *flat* face normal (before) and *smoothed* shading normal (after).

| metric | flat (before) | smoothed (after) |
| --- | --- | --- |
| decimated sphere (48×32 → 500 faces), mean normal deviation | 3.55° | **2.06°** |

On a cube the smoothing groups keep all three normals at each corner distinct, so
creases stay crisp while flat sides are untouched (`status: unchanged` when there
is nothing to smooth). The Phase D4 completion criterion — normal deviation
improves — is the offline assertion `improvement_deg > 0` on a curved LOD.

> Note: Blender's geometric `poly.normal` (what the shape evaluator reads) is
> unaffected by custom split normals, so the cleanup improves *viewport shading*
> rather than the geometric `shape_report.json`. The shading improvement is what
> `geometry/normals.py` measures.

## How Quad Retopology Works

This project treats **quad retopology** as a separate mode from
ZBrush-style decimation. Quad retopo is used when the result needs cleaner
topology, no n-gons, mostly/all quads, and more natural mesh flow. It is the
right mode for assets that will later go through UV unwrapping, baking, manual
cleanup, or deformation-aware editing.

The high-level process is:

```text
high-poly mesh
  -> feature analysis
  -> target face count / topology level selection
  -> quad-oriented remesh
  -> shrinkwrap/project to high-poly
  -> topology validation
  -> shape validation
  -> quad-flow scoring and cleanup
```

### 1. Analyze features first

Before generating the low-poly, the worker analyzes hard edges, boundaries,
dihedral angles, curvature-like regions, and material boundaries. These features
identify where the mesh should keep more structure.

Use:

```bash
--preserve-features true
--feature-angle 30
```

On the Blender path, feature preservation marks important edges as sharp before
QuadriFlow attempts, and can steer voxel fallback with adaptivity. This keeps
panel lines, silhouettes, and high-curvature regions from being treated the same
as flat areas.

### 2. Prefer QuadriFlow for true quad retopo

The preferred quad path is Blender's QuadriFlow remesh:

```text
QuadriFlow Remesh
  -> target_faces retry
  -> preserve sharp/boundary edges
  -> shrinkwrap to source
```

QuadriFlow is the only Phase 1-6 generator path that is explicitly
quad-oriented. The worker retries `target_faces` because Blender treats it as a
hint rather than an exact output count.

For very large inputs, QuadriFlow is skipped by default:

```text
source faces > 1,500,000
  -> skip QuadriFlow
  -> use voxel fallback
```

That skip avoids multi-million-face cases where QuadriFlow is impractically slow
or fails silently. The fallback can still produce all-quad meshes in Blender 5,
but it is not the same as production-quality manual quad retopology.

### 3. Use voxel fallback as a practical quad-ish generator

When QuadriFlow is unavailable or unsuitable, the worker uses Voxel Remesh with a
binary-searched voxel size:

```text
actual < target
  -> reduce voxel size
  -> more faces

actual > target
  -> increase voxel size
  -> fewer faces
```

This gets the output near the target face count and often produces quad-only
faces in Blender 5. It is practical for huge sculpt meshes, but it may not
preserve semantic edge flow. Treat it as an automatic quad-like base mesh, not as
final hand-authored topology.

### 4. Project back to the high-poly

After remeshing, the result is shrinkwrapped back onto the original high-poly:

```text
low-poly candidate
  -> Shrinkwrap modifier
  -> nearest surface projection
```

This step keeps the generated low-poly close to the original surface. Shape
quality is then measured by `shape_report.json`.

### 5. Validate topology

Quad retopo mode gates the result with stricter topology rules:

```text
ngon_count = 0
quad_ratio >= 0.98 for accepted
triangle_ratio <= 0.02 for accepted
non_manifold_edge_count = 0
target_error_ratio <= 0.15 for accepted
```

The worker writes `validation_report.json`. If a result has triangles or n-gons,
it can still be useful for decimation mode, but it is not accepted as a clean
quad retopo result.

### 6. Improve and score quad flow

Phase 6 scores mesh flow using:

```text
quad fraction
valence regularity
face squareness
```

Optional cleanup is enabled with:

```bash
--improve-quad-flow true
--quadflow-smooth-iterations 5
```

The cleanup tries triangle-to-quad conversion, relaxes vertices, pins features
and boundaries, then re-projects to the high-poly. The report is written to
`quadflow_report.json`.

### Recommended command

For quad retopo mode:

```bash
blender --background input.blend \
  --python worker/run_retopo_job.py -- \
  --provider mock \
  --mode quad_retopo \
  --object-name HighPolyObject \
  --target-face-count 10000 \
  --quad-required true \
  --ngon-allowed false \
  --preserve-features true \
  --feature-angle 30 \
  --apply-shrinkwrap true \
  --improve-quad-flow true
```

### When not to use quad retopo mode

If the user asks for "ZBrush Decimation", "just make it lighter", "LOD mesh", or
"keep the shape but triangles are fine", use `mode=decimation_optimize` instead.
That mode should allow triangles and optimize for shape preservation rather than
edge flow.

## Running

Offline (no Blender), e.g. in a REPL or test:

```python
from retopo_agent.io.fixtures import build_uv_sphere
from retopo_agent.geometry.decimate import decimate_to_target

high = build_uv_sphere(segments=420, rings=260)  # ~109k faces
print(decimate_to_target(high, 10000).to_dict())
# -> ~9968 faces, target_error_ratio ~0.003 (within the "accepted" band)
```

Inside Blender (plan §15.12):

```bash
blender --background input.blend \
  --python worker/run_retopo_job.py -- \
  --provider mock \
  --object-name HighPolyObject \
  --target-face-count 10000 \
  --topology-level low_retopo \
  --quad-required true \
  --ngon-allowed false
```

Outputs to `out/<job_id>/`: `retopo_plan.json`, `generation_report.json`,
`validation_report.json`, `shape_report.json`, `feature_report.json`,
`quadflow_report.json`, and (best-effort) `lowpoly.blend` / `lowpoly.fbx` /
`preview.png` (with `--render-preview true`). Add `--preserve-features true
--voxel-adaptivity 0.5` for feature-aware generation, and `--improve-quad-flow
true` for quad-flow cleanup.

Batch LODs (Phase 4) — pass `--levels` and/or `--targets`:

```bash
blender --background input.blend \
  --python worker/run_retopo_job.py -- \
  --provider mock --object-name HighPolyObject \
  --targets 50000,20000,10000          # or: --levels high_retopo,mid_retopo,low_retopo
```

Outputs `comparison.json` + `lowpoly_lods.blend` / `lowpoly_lods.fbx`.

## Tests

```bash
python3 -m pytest tests/test_retopo_decimate.py tests/test_retopo_target_search.py \
                  tests/test_retopo_validate.py tests/test_retopo_shape_eval.py \
                  tests/test_retopo_levels.py tests/test_retopo_features.py \
                  tests/test_retopo_quadflow.py -q
```
