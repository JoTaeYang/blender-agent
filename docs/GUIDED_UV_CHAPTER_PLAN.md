# Guided UV Chapter Flow

## Goal

The desired end-to-end flow is: (1) an OBJ is given, (2) the agent inspects the model
visually via Blender MCP, (3) it proposes — per body part — how a *human artist* would
split the UV into "chapters", and (4) once the user approves, it generates UVs from that
judgement.

This work delivers the **deterministic back half** (steps 3→4 mechanics) BEFORE the
auto-drafting/approval UX: pin a human/agent part judgement as an explicit `chapter_spec`
and verify we can build correct UVs from it. Auto-drafting the spec from screenshots +
mesh inspection (step 3 automation) is follow-up.

This is a **hybrid**: `chart_uv_agent` alone has weak artist-style part judgement;
`artist_uv_agent` alone has weak hard correctness. Guided takes the artist semantic part
layer and the chart hard-correctness layer (mandatory ≥90° folds, no overlap, forbidden
edges) together.

## Architecture

New module `artist_uv_agent/guided.py`. Everything down to `build_guided_seams` is pure /
Blender-free (unit-tested without `bpy`); `run_guided_uv` is the Blender entry point.

```
A1 segment_parts → split_branched_parts → A2 describe_parts → A3 classify_parts   (reused)
  → assign_chapters         spec part-ids → chapters; uncovered parts → class fallback
  → build_guided_seams      part-boundary floor; dissolve same-chapter boundaries;
                            strip non-mandatory forbidden; re-assert mandatory union;
                            per-chapter seam policy; diskify every chart
  → run_guided_uv (Blender) SLIM unwrap + CONCAVE pack → MINIMAL hard-gate repair loop →
                            forbidden strip → aux-seam prune → chart hard/quality gate +
                            report (chart→chapter map RE-COMPUTED from the final seams)
```

### Hard-gate repair loop (run_guided_uv)

The guided engine does not merely *gate* correctness — it repairs it, minimally and without
broadly re-charting the guided seams. Each round applies the single highest-priority fix and
re-SLIMs (mirrors the chart engine's main loop):

1. a mandatory fold welded in the UV → LOCAL, forbidden-aware min-cost cut
   (`split_welded_folds`), tagged `welded_fold_auxiliary`;
2. a flipped face / self raster-overlap → split the folding chart (`overlap_repair`); a rare
   inter-chart invasion → one margin-bump / AABB re-pack;
3. global or worst-island checker distortion over bar → split the ONE worst island
   (`distortion_repair`).

Then non-mandatory forbidden edges are stripped (preserve wins; any failure that re-creates
is reported honestly, not hidden), auxiliary low-angle seams are pruned while every hard gate
stays green, and the **chart→chapter map / report / overlays are recomputed from the final
seams** so repairs never leave them stale.

`mandatory_fold_angle` from the spec is threaded through the audits and the gate's reported
limit (not hardcoded to 90°), so a non-90° spec is reported and gated correctly.

### Segmentation modes (compute only what the spec needs)

The deep artist watershed (`segment_parts`) over-segments a decimated asset — the
5850-face `humanstatue_low` becomes **306 parts** and the watershed + `_merge_weak_parts`
(O(parts²·E) pure-Python Dijkstra) takes minutes. When the spec has no hand-filled
`source_part_ids` there is nothing to validate against that decomposition, so guided uses a
**coarse fast path** instead:

| `segmentation_mode` | part table |
|---------------------|-----------|
| `auto` (default) | `coarse` when no chapter fills `source_part_ids`, else `full` |
| `coarse` | connected-component shells (`coarse_segment_parts`, O(F+E), no Dijkstra/merge/descriptors) → 11 parts on the statue, **~0ms** |
| `full` / `manual_parts` | deep artist watershed (so hand-filled part ids resolve) |

`run_guided_uv(..., segmentation_mode=...)` (or `--segmentation-mode`) overrides the spec.
Coarse parts get the `coarse` chapter class → keep-intact (diskify only, no stretch
cone-split). On the statue the whole pure spec→seam build now runs in **~1.9s** (was minutes)
with `mandatory_90_missing == 0` and edge 3054 preserved.

Perf fixes that make this practical: `MeshGraph.face_adjacency()` is now lazily **cached**
(was rebuilt O(E) per call, called thousands of times), `_diskify_and_split` takes a
`max_diskify_rounds` budget (leftover non-disks reported, never silently shipped) and uses a
cheap Euler disk precheck (skips the per-vertex wedge union-find for charts with no interior
seam slit — provably equivalent).

### Guided-intent coverage (an empty spec must not read as success)

`guided_uv_report.json` carries a `coverage` block + `guided_intent_applied` flag so a
reviewer can tell whether the artist's part judgement actually drove the result or it shipped
via fallback:

- `guided_intent_applied` — False when no spec chapter resolved parts (all fallback).
- `spec_chapter_face_coverage`, `fallback_face_ratio` — face fractions guided vs fallback.
- `unresolved_spec_chapters` — spec chapters with empty `source_part_ids`.
- `template_chapter_count`, `front_preserve_chapter_count`, `keep_intact_chapter_count`,
  `organic_split_chapter_count` — how many resolved chapters use each policy.
- `report["warnings"]` includes "artist-guided intent NOT applied: …" and lists unresolved
  chapters; the worker echoes them as `[P5] guided WARN:`.

Empty-spec coarse run on humanstatue_low: `fallback_face_ratio=1.0`,
`guided_intent_applied=False` → flagged, not a false success. Rough-mapped run (11 coarse
components → chapters, `.context/chapter_spec_humanstatue_coarse.json`):
`spec_chapter_face_coverage=1.0`, `fallback_face_ratio=0.0`, `guided_intent_applied=True`.

**Intent assigned ≠ policy reflected.** `guided_intent_applied` is ASSIGNMENT coverage only
(spec resolved parts, fallback ratio low). Whether each chapter's SEAM policy actually fired
is reported SEPARATELY so a "cylinder chapter count" can never be mistaken for "N tube strips
produced" (review): `report["policy_reflection"]` carries `cylinder_policy_chapter_count`
(REQUESTED), `template_policy_applied_count` + `chapter_template_seam_count` (ACTUALLY fired),
and `unreflected_policy_chapters`; `report["guided_policy_reflected"]` is True only when every
resolved cylinder chapter fired its template. A cylinder chapter whose template reverted on
non-tube geometry emits a "cylinder policy NOT reflected" warning.

**`cylinder_group` branch-split.** A `cylinder_group` chapter branch-splits its part at the
fork (shaft + prongs) BEFORE opening each sub-tube, so a staff actually becomes a set of
strips instead of one un-openable blob. On the applied statue (`.context/apply_guided_uv.py`
→ `.context/guided_applied/humanstatue_low_guided.blend`, Blender 5, 5.4s, gate accepted):
the `staff` (cylinder_group) fired → `chapter_template=41` seams, `templates_applied=1`; the
`sleeve` (plain `cylinder`, a single non-tube coarse component) did NOT → `unreflected=['sleeve']`,
`guided_policy_reflected=False`, warned. Hard gates all pass (`mandatory_90_uv_unsplit=0`,
`overlap=0`, `raster=0.00035`, 3054 preserved). So the tube-strip policy now reflects where
the geometry is a real (forked) tube, and is honestly flagged where it is not.

### Completion status — UV-technical success vs guided-judgement success

Two distinct successes are reported (work plan §1), never conflated:
- `uv_shippable` — the hard UV gate passed (correctness). `== shippable` (kept for compat).
- `guided_complete` — `uv_shippable` AND every chapter policy reflected.
- `completion_status` — `guided_complete` | `accepted_with_policy_warning` (valid UV, some
  policy unreflected) | `valid_fallback_uv` (no spec intent) | `failed_gate`.

Applied statue (`.context/guided_applied_v2/`, mode=coarse, Blender 5): **`uv_shippable=True`,
`guided_complete=False`, `completion_status=accepted_with_policy_warning`** — gate accepted
(`mandatory_90_uv_unsplit=0`, `overlap=0`, `raster=0.00043`, 3054 preserved), staff
`cylinder_group` fired (`chapter_template=41`), sleeve honestly `unreflected`, front-preserve
active.

### Tube opener escalation (work plan §2)

A `cylinder` chapter escalates its opener: clean lengthwise `cylinder_template` →
`open_multiloop_tube` (connects N boundary loops; opens a decimated 3-loop sleeve a 2-loop
template can't) → `cylinder_group` branch-split. On the statue the staff opens via
branch-split; the sleeve (a fragmented coarse component with internal mandatory folds) cannot
form one clean strip and stays honestly `unreflected` — `accepted_with_policy_warning`, not a
false success.

### Front-preserve — real, disk-safe, gate-yielding (work plan §3)

When `spec.front_preserve_axis` (e.g. `"+Z"`) is set, every `organic_front_preserve` chapter's
front-facing (`mean normal · axis > threshold`) LOW-angle (`dihedral < max_dihedral`),
non-mandatory interior edge is auto-preserved so no seam crosses the visible face/robe front.
It is a SOFT preserve with a strict priority order so it can NEVER break the hard gate:

1. mandatory ≥90° folds are excluded (always cuttable);
2. a front edge that is a load-bearing diskify cut is NOT preserved (kept as a cut) →
   `front_preserve_disk_conflict_count` (disk topology wins);
3. the welded-fold repair avoids front edges (forbidden = hard preserve ∪ front);
4. but a distortion/overlap repair MAY cut a front edge to pass the hard gate → that edge is
   `front_preserve_relaxed` (the hard gate outranks soft preserve), reported + warned.

Report: `front_preserve_protection` (`active_view_axis` | `requested_no_edges` |
`label_only_no_auto_front_edges`), `front_preserve_edge_count` (final preserved),
`front_preserve_disk_conflict_count`, `front_preserve_relaxed_count`, `front_preserve_axis`.
On the statue: axis `+Z`, **468 front edges preserved, 24 disk-conflict, 2 relaxed**, gate
still accepted. With no axis it is honestly `label_only` + warning (not silent success).

### Artist-intent checklist (v3 — fidelity, not just gate)

`guided_uv_report.json` carries an `artist_intent_checklist` mapping the 10 canonical worker
judgements (staff, face, hood, upper_front_robe, back_cloak, sleeve, hands, belt, lower_robe,
feet) to a per-intent status (`passed` | `partial` | `failed` | `missing`), plus
`artist_intent_passed`, `unmet_artist_intents`, and `face_policy`. A `failed` intent (chapter
present, policy not reflected) is always unmet; a `missing` intent counts only when the worker
DECLARED it via `spec.expected_intents` (a minimal spec is not penalised for intents it never
raised). `guided_complete = uv_shippable AND guided_policy_reflected AND artist_intent_passed`
— so a valid UV with unmet judgements is `accepted_with_policy_warning`, never a false success.
The top-level `uv_shippable` / `guided_complete` / `completion_status` / `artist_intent_passed`
/ `unmet_artist_intents` are mirrored into `p5_gate.json`.

### Selector — split one part into chapters by normal axis (v3)

`GuidedChapter.selector = {"normal_axis": "-Z", "threshold": 0.35}` carves a face SUBSET of the
source parts (front-facing vs back-facing), so two chapters can split one shell — e.g.
`upper_front_robe` (+Z) and `back_cloak` (−Z) from the torso part. Implemented as a pre-step
(`apply_chapter_selectors`) that carves new parts and rewrites `source_part_ids`, leaving the
part-based pipeline unchanged. The unselected remainder (sides) → fallback chapter.

### Face front island (v3)

`type: "face_front_preserve"` is front-preserve seam behaviour reported under a dedicated
`face_policy` (`front_smooth_seam_count` — front-facing low-dihedral seams crossing the face;
`face_beard_chart_count`; `status`). A back-biased pre-open (`open_multiloop_tube` with
`back_dir = -front_axis`) routes a multi-loop face chart's disk cut to the back of the head.

**Limitation (honest).** On the decimated statue the face part is a near-closed cap (<2
boundary loops), so the back-opener is a no-op and 26 front cuts are genuinely load-bearing
for disk topology (`front_preserve_disk_conflict_count` ≈ 24) → `face_policy.status = "failed"`,
`face` in `unmet_artist_intents`. Eliminating them needs a COST-BASED disk cutter (route the
opening seam along back/side/chin via `edge_cut_cost`, not the normal-VSA `split_chart`) — the
documented next step. The sleeve (internal ≥90° folds fragment the coarse component — a single
clean tube is geometrically impossible, mandatory folds must stay) and hands (no hand part in
the coarse decomposition) are likewise blocked on this coarse asset; all three are honestly
carried in `unmet_artist_intents` rather than faked.

### v3 applied result (`.context/guided_applied_v3/`, mode=coarse, Blender 5)

`uv_shippable=True`, `guided_complete=False`, `completion_status=accepted_with_policy_warning`,
`artist_intent_passed=False`, `unmet_artist_intents=['face','hands','sleeve']`. Gate accepted
(`mandatory_90_uv_unsplit=0`, overlap 0, raster 0.00034, 3054 preserved, nondisk 0). Met: staff
(cylinder_group strip), back_cloak (selector split present), front-preserve active (468 edges).
Unmet (honest): face front island (26 crossing seams), sleeve tube (fragmented coarse part),
hands (no hand part in coarse → needs targeted split). The repair hard cap scales to the
starting chart count so selector/face structure can't starve the distortion repair.

### chapter_spec

`GuidedUVSpec` { `version`, `object`, `forbidden_edges`, `mandatory_fold_angle`,
`chapters[]` }; `GuidedChapter` { `name`, `source_part_ids`, `type`, `seam_policy` }.
Load/dump via `from_dict`/`to_dict`/`from_json`/`to_json`/`coerce` (accepts spec, dict, or
JSON string). `source_part_ids` are hand-filled in v1 (auto part→chapter mapping is
follow-up). An **unknown chapter type is never an error** — it falls back to the chart
organic split, so the spec can name new chapters freely.

### chapter type → seam behaviour

| type | behaviour |
|------|-----------|
| `cylinder` | `cylinder_template` (cap separation + one lengthwise cut → rectangle strip) |
| `cylinder_group` | per source-part `cylinder_template` (branched staff/prongs) |
| `cloth_panel`, `panel`, `strip`, `cap`, `detail` | keep intact (diskify only) |
| `cloth_panels` | organic cone-split on deep valleys / creases |
| `organic_front_preserve` | keep intact, no front seams (relies on forbidden + back-biased repair) |
| `blob`, `shell`, `unknown`, any other | chart organic split (fallback) |

Parts the spec does not cover become their own **class-based fallback chapter**, so
generation always completes on a partial spec.

### Forbidden / no-cut edges

`spec.forbidden_edges` are preserved end-to-end: a non-mandatory forbidden edge never ships
as a seam (stripped from the floor, never re-added — the welded-fold repair routes around
it via `edge_cut_cost`). A forbidden edge that is ALSO a mandatory ≥`fold_angle` fold is a
reported **conflict** — the fold wins (a hard crease must stay a seam). The selected robe
edge `3054` (low-angle smooth surface, normal angle 15.6°) is the motivating case: it must
be forbidden and absent from the final seams.

Preserve can also be incompatible with **disk topology**: `split_chart` (diskify) is not
forbidden-aware, so it may transiently re-add a forbidden edge — which is then stripped
again. If stripping leaves a chart genuinely non-disk, that chart id is reported in
`nondisk_charts` and the load-bearing forbidden edges in `forbidden_disk_conflicts` (a
non-disk chart self-folds in SLIM → surfaced honestly by the overlap gate, never silently
shipped). A transiently-re-added edge whose removal still leaves a valid disk is NOT a
conflict and is not reported.

### Seam taxonomy (reported)

`chapter_boundary`, `chapter_template`, `mandatory_90`, `welded_fold_auxiliary`,
`overlap_repair`, `distortion_repair`, `fallback_segmentation`, `user_forbidden`
(should never ship).

## Outputs

- `guided_parts.json` — auto part table + chapter assignment + segmentation history.
- `guided_uv_report.json` — final metrics, gate, per-chapter chart correspondence,
  forbidden conflicts/stripped, seam-type counts.
- `guided_uv_colored_by_chapter.{png,svg}` — chapter-coloured UV overlay.

## Worker wiring

`--uv-engine guided [--chapter-spec <path.json>] [--forbidden-edges 3054]`. The CLI
forbidden set is merged into the spec's. With no `--chapter-spec`, an empty (all-fallback)
spec is used.

Manual Blender harness: `.context/guided_uv_run.py` runs `run_guided_uv` on the open
object and prints the eight acceptance checks (incl. 3054). Example spec:
`.context/chapter_spec_5850.json`.

## Test criteria (GUIDED_UV_CHAPTER_PLAN §test)

1. `forbidden_edges={3054}` → 3054 absent from final seams — `test_forbidden_*` (seam-set),
   `.context/guided_uv_run.py` (real model).
2. 3054 (non-mandatory) → no removal conflict — `test_forbidden_nonmandatory_*`.
3. `mandatory_90_uv_unsplit == 0` — Blender (worker / `.context` run).
4. `mandatory_90_missing == 0` — `test_mandatory_folds_are_all_seams`.
5. overlap / raster overlap hard gate pass — Blender; disk invariant proxy in
   `test_every_chart_is_a_uv_disk`.
6. staff shaft/prong → rectangular strip, not blob — `test_cylinder_chapter_*`.
7. chapter count / report matches the spec — `test_assignment_*`, `test_chart_to_chapter_*`.
8. uncovered parts still produce UVs (fallback) — `test_uncovered_*`, `test_empty_chapter_*`.

Blender-free suite: `python3 -m pytest tests/test_artist_uv_guided.py`. UV-level criteria
(3, 5 raster) run in Blender via the worker or `.context/guided_uv_run.py`.
