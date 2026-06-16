# Interactive Chapter Seam Planning

## Why

The guided UV engine (`artist_uv_agent/guided.py`) tries to judge the *whole* object at once
from a `chapter_spec`. On coarse / messy assets (`sample/humanstatue_low.obj`) that rarely
reaches `guided_complete=True` — face, sleeve, hands don't separate cleanly, so the v3 run
ships valid UVs (`uv_shippable=True`) but with unmet artist intent (face front still had ~26
front-smooth seams).

So the guided engine is **kept as a deterministic back-end** and a new **interactive
front-end** drives it one body part at a time:

```
observe part → propose seam plan (draft) → user approves/edits → save chapter constraint → next part
```

When the parts that matter are approved:

```
approved chapters → GuidedUVSpec → run_guided_uv → final UV → per-chapter constraint report
```

Success here is **not** `guided_complete=True`; it is that the per-part planning loop works and
reports honestly whether the result kept each approved rule.

## What shipped

| Piece | Where | Tested |
|---|---|---|
| Data model (`InteractiveUVPlan` / `InteractiveChapterPlan` / `ChapterSource` / `ChapterIntent` / `ChapterConstraints` / `ObservationSummary`) | `artist_uv_agent/interactive_plan.py` | `tests/test_artist_uv_interactive.py` |
| `observe_chapter` (pure mesh stats + risk flags; optional Blender screenshots) | same | yes (headless) |
| `draft_seam_plan` + per-kind `CHAPTER_TEMPLATES` | same | yes |
| `to_guided_spec` (APPROVED chapters only) | same | yes |
| `evaluate_interactive_constraints` (per-chapter verification) | same | yes |
| guided wiring: `run_guided_uv(..., interactive_plan=)`, `build_guided_report(..., interactive=)` | `artist_uv_agent/guided.py` | yes |
| CLI: `observe / draft / approve / reject / revise / status / export-guided-spec / verify` | `artist_uv_agent/interactive_cli.py` | end-to-end run |
| Blender runner (full unwrap + interactive block) | `.context/apply_interactive_uv.py` | needs Blender |
| Blender observe + screenshots | `.context/interactive_observe_face_blender.py` | needs Blender |

### Files written during a session

```
.context/interactive_uv_plan.json                 accumulating approved plan (source of truth)
.context/interactive_uv_observations/<chapter>.json   measured stats (+ screenshots from Blender)
.context/interactive_uv_drafts/<chapter>_draft.json   proposed plan before approval
.context/interactive_chapter_spec.json            exported GuidedUVSpec (approved only)
.context/interactive_constraint_report.json       per-chapter constraint verdicts
.context/interactive_applied/                      full Blender run output (report/parts/gate/overlays/.blend)
```

## Constraints that are machine-checked

`evaluate_interactive_constraints` measures these against the FINAL seams; everything else in a
chapter's `constraints` is reported `checkable:false` (advisory) — never silently "passed".

- `max_front_smooth_seams` / `max_front_center_seams` — front-facing low-dihedral seams on the
  part (centre = central 50% band along the lateral axis). **Seam-level → checkable headless.**
- `min_panel_count` / `max_panel_count` — island count for the part. Seam-level.
- `mandatory_folds_must_split` — every ≥fold-angle fold inside the part is a seam. Seam-level.

`interactive_constraints_passed` = all *checkable* constraints passed **and** every approved
chapter resolved. It is ANDed into `guided_complete`; a shippable UV that breaks an approved
rule reports `completion_status="accepted_with_unmet_interactive_constraints"` and a warning
naming `chapter.rule`.

**Unresolved approved chapter = failure.** An approved chapter that does not map to a guided
chapter with actual faces (dropped name, empty/invalid `source_part_ids`/`source_face_ids`) is
reported `chapter_resolved:false`, listed in `unresolved_approved_chapters`, and forces
`interactive_constraints_passed:false` — an approved judgement that never reached the result
must never read as success.

**Face-set selection survives to the backend.** A chapter may be selected by explicit
`source_face_ids` (absolute mesh face ids), not just coarse `part_ids`. `to_guided_spec` carries
them into the `GuidedChapter`, and `build_guided_assignment` → `apply_chapter_face_selection`
carves those faces into their own part *before* assignment, so the face-set actually drives the
part-based seam construction. `build_guided_assignment` is the single preparation
(segment → face-set carve → selector carve → assign) shared by `run_guided_uv` and the headless
`verify`, so a `source_face_ids` / `selector` chapter resolves identically with and without
Blender.

## First flow result (face, headless)

```
observe face (coarse parts 2,3): faces=1193 boundary_loops=2 mandatory_folds=20
                                 front_smooth_edges=494  risk=high_front_smooth_edge_density
draft   face: type=face_front_preserve, max_front_smooth_seams=0, mandatory_folds_must_split=True
approve face → exported spec → verify (seam-level, pre-unwrap):
  [FAIL] max_front_smooth_seams: expected=0 actual=24
  [FAIL] max_front_center_seams: expected=0 actual=23
  [PASS] mandatory_folds_must_split
```

The FAIL is the point: the loop surfaces exactly what the v3 auto run hid behind a passing hard
gate. `verify` is seam-level (front-smooth/panel/fold are all seam properties); overlap and
distortion need the Blender unwrap (`.context/apply_interactive_uv.py`).

## How to run

```bash
# headless planning loop (no Blender)
python3 -m artist_uv_agent.interactive_cli --front-axis +Z --up-axis +Y --forbidden-edges 3054 \
    --object humanstatue_low observe --obj sample/humanstatue_low.obj --chapter face --kind face --source-parts 2,3
python3 -m artist_uv_agent.interactive_cli draft   --chapter face --kind face
python3 -m artist_uv_agent.interactive_cli approve --chapter face
python3 -m artist_uv_agent.interactive_cli export-guided-spec
python3 -m artist_uv_agent.interactive_cli verify  --obj sample/humanstatue_low.obj

# full UV + constraint check (Blender)
/Applications/Blender.app/Contents/MacOS/Blender --background --python .context/apply_interactive_uv.py
# observation screenshots (Blender)
/Applications/Blender.app/Contents/MacOS/Blender --background --python .context/interactive_observe_face_blender.py
```

## Next chapters (recommended order, plan §권장 부위 진행 순서)

face/beard → hood/head-back → staff → hands → sleeve → upper front robe → back cloak → belt →
lower robe → feet. Each has a `CHAPTER_TEMPLATES` entry; observe + draft + approve incrementally.

## Status / open items

- Pure core (data model, draft, export, constraint check, CLI) is unit-tested and runs headless.
- The **full Blender run** (`apply_interactive_uv.py`) has not been executed here (no Blender in
  this environment). It reuses the already-green `run_guided_uv` path; the only new code on that
  path is the `evaluate_interactive_constraints` call + report embed, both unit-tested.
- The face constraint **fails today** (front protection can't keep all front edges unbroken on
  the coarse statue). Reducing it is engine work (stronger back-of-head reroute in
  `face_front_preserve`), separate from the planning loop, which is the deliverable here.
- `max_front_center_seams` uses a geometric centre band; refine the lateral-axis heuristic if an
  asset isn't `+Y` up / `+Z` front.
