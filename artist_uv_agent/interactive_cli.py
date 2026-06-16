"""Interactive chapter-seam-planning CLI (GUIDED_UV_CHAPTER_PLAN §작업4).

Drives the per-part planning loop from the shell, persisting everything under ``.context``:

    observe → draft → approve → export-guided-spec → verify → (next chapter)

    python3 -m artist_uv_agent.interactive_cli observe \
        --obj sample/humanstatue_low.obj --chapter face --kind face --source-parts 2,3
    python3 -m artist_uv_agent.interactive_cli draft   --chapter face --kind face
    python3 -m artist_uv_agent.interactive_cli approve --chapter face
    python3 -m artist_uv_agent.interactive_cli export-guided-spec
    python3 -m artist_uv_agent.interactive_cli verify  --obj sample/humanstatue_low.obj

Headless throughout (the OBJ is read with :func:`uv_agent.io.obj_loader.load_obj`); the only
Blender steps are the optional observation screenshots and the final SLIM unwrap, which live in
``.context/apply_interactive_uv.py``. ``verify`` checks the APPROVED constraints on the
deterministic seam set (front-smooth / panel-count / mandatory folds are all seam-level), so
the planning loop is fully exercisable without Blender; only overlap/distortion need the unwrap.
"""

from __future__ import annotations

import argparse
import json
import os

from artist_uv_agent.interactive_plan import (
    ChapterSource, InteractiveChapterPlan, InteractiveUVPlan, ObservationSummary,
    describe_plan_for_approval, draft_seam_plan, evaluate_interactive_constraints,
    observe_chapter,
)

DEFAULT_PLAN = ".context/interactive_uv_plan.json"
OBS_DIR = ".context/interactive_uv_observations"
DRAFT_DIR = ".context/interactive_uv_drafts"
DEFAULT_SPEC_OUT = ".context/interactive_chapter_spec.json"
DEFAULT_REPORT_OUT = ".context/interactive_constraint_report.json"


# --- shared helpers ---------------------------------------------------------------------

def _load_mesh(path: str):
    from uv_agent.io.obj_loader import load_obj
    return load_obj(path)


def _load_or_init_plan(args) -> InteractiveUVPlan:
    if os.path.exists(args.plan):
        plan = InteractiveUVPlan.load(args.plan)
    else:
        plan = InteractiveUVPlan(object=getattr(args, "object", "") or "")
    # CLI axis flags override the stored plan only when explicitly given.
    for attr in ("front_axis", "up_axis", "segmentation_mode"):
        v = getattr(args, attr, None)
        if v:
            setattr(plan, attr, v)
    if getattr(args, "forbidden_edges", None):
        plan.forbidden_edges = sorted({int(x) for x in args.forbidden_edges.split(",") if x})
    return plan


def _source_from_args(args) -> ChapterSource:
    part_ids = [int(x) for x in (args.source_parts or "").split(",") if x.strip()]
    face_ids = [int(x) for x in (args.source_faces or "").split(",") if x.strip()]
    sel = "face_ids" if face_ids else "part_ids"
    return ChapterSource(selection_type=sel, part_ids=part_ids, face_ids=face_ids)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- commands ---------------------------------------------------------------------------

def cmd_observe(args) -> int:
    plan = _load_or_init_plan(args)
    mesh = _load_mesh(args.obj)
    src = _source_from_args(args)
    obs = observe_chapter(
        mesh, None, args.chapter, src, front_axis=plan.front_axis, up_axis=plan.up_axis,
        threshold=plan.front_preserve_threshold, max_dihedral=plan.front_preserve_max_dihedral,
        fold_angle=plan.mandatory_fold_angle, observed_at=_now())
    os.makedirs(OBS_DIR, exist_ok=True)
    out = os.path.join(OBS_DIR, f"{args.chapter}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(obs.to_dict(), fh, indent=2)
    # persist the asset axes / object so later commands need no re-typing
    if not plan.object:
        plan.object = os.path.splitext(os.path.basename(args.obj))[0]
    plan.save(args.plan)
    print(f"[observe] {args.chapter}: faces={obs.face_count} boundary_loops={obs.boundary_loop_count} "
          f"mandatory_folds={obs.mandatory_fold_count} front_smooth_edges={obs.front_smooth_edge_count} "
          f"islands={obs.current_chart_count}")
    print(f"          normals={obs.normal_axis_histogram}")
    if obs.risk_flags:
        print(f"          RISKS: {obs.risk_flags}")
    print(f"          → {out}")
    return 0


def cmd_draft(args) -> int:
    plan = _load_or_init_plan(args)
    obs_path = os.path.join(OBS_DIR, f"{args.chapter}.json")
    if not os.path.exists(obs_path):
        print(f"[draft] no observation for '{args.chapter}'. Run `observe --chapter {args.chapter}` "
              f"first.")
        return 2
    obs = ObservationSummary.from_dict(json.load(open(obs_path, encoding="utf-8")))
    prefs = json.loads(args.preferences) if args.preferences else None
    draft = draft_seam_plan(obs, args.kind or args.chapter, artist_preferences=prefs)
    os.makedirs(DRAFT_DIR, exist_ok=True)
    with open(os.path.join(DRAFT_DIR, f"{args.chapter}_draft.json"), "w", encoding="utf-8") as fh:
        json.dump(draft.to_dict(), fh, indent=2)
    plan.upsert_chapter(draft)           # status stays "draft" until `approve`
    plan.save(args.plan)
    print("[draft]")
    print(describe_plan_for_approval(draft))
    print(f"  → saved draft (status={draft.status}); approve with "
          f"`approve --chapter {args.chapter}`")
    return 0


def _set_status(args, status: str) -> int:
    plan = _load_or_init_plan(args)
    if plan.get_chapter(args.chapter) is None:
        print(f"[{status}] no chapter '{args.chapter}' in {args.plan}")
        return 2
    plan.set_status(args.chapter, status, user_notes=args.notes)
    plan.save(args.plan)
    ch = plan.get_chapter(args.chapter)
    print(f"[{status}] {args.chapter} → status={ch.status} (rev {ch.revision})")
    return 0


def cmd_approve(args) -> int:
    return _set_status(args, "approved")


def cmd_reject(args) -> int:
    return _set_status(args, "rejected")


def cmd_revise(args) -> int:
    return _set_status(args, "needs_revision")


def cmd_status(args) -> int:
    plan = _load_or_init_plan(args)
    print(f"object={plan.object} front_axis={plan.front_axis} up_axis={plan.up_axis} "
          f"mode={plan.segmentation_mode} forbidden={plan.forbidden_edges}")
    if not plan.chapters:
        print("  (no chapters yet — run `observe`)")
        return 0
    for c in plan.chapters:
        flag = "✓" if c.status == "approved" else " "
        print(f"  [{flag}] {c.name:18s} {c.status:14s} rev{c.revision} type={c.guided_type} "
              f"parts={c.source.part_ids or c.source.face_ids}")
    print(f"  approved: {[c.name for c in plan.approved_chapters()]}")
    return 0


def cmd_export_guided_spec(args) -> int:
    plan = _load_or_init_plan(args)
    intents = [x for x in (args.expected_intents or "").split(",") if x.strip()] or None
    spec = plan.to_guided_spec(expected_intents=intents)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(spec.to_json())
    print(f"[export] {len(spec.chapters)} approved chapter(s) → {args.out}")
    for c in spec.chapters:
        print(f"  {c.name:18s} type={c.type:20s} parts={c.source_part_ids} "
              f"selector={c.selector}")
    return 0


def cmd_verify(args) -> int:
    """Headless verify: build the deterministic seam set for the APPROVED chapters and check
    each chapter's constraints on it (front-smooth / panel-count / mandatory folds). The full
    overlap/distortion gate needs Blender — see ``.context/apply_interactive_uv.py``."""
    from artist_uv_agent.guided import (
        build_guided_assignment, build_guided_seams, map_charts_to_chapters,
    )

    plan = _load_or_init_plan(args)
    mesh = _load_mesh(args.obj)
    spec = plan.to_guided_spec()
    if not spec.chapters:
        print("[verify] no approved chapters to verify.")
        return 2
    # Same preparation as run_guided_uv: segment + carve face-sets/selectors + assign, so a
    # source_face_ids / selector chapter resolves here exactly as it will in the Blender run.
    seg, descriptors, classes, spec, assignment = build_guided_assignment(mesh, spec)
    built = build_guided_seams(mesh, seg, descriptors, classes, spec, assignment)
    _, _, cc = map_charts_to_chapters(mesh, built.seams, seg.face_part, assignment.part_chapter)
    block = evaluate_interactive_constraints(
        plan, mesh, assignment, cc, built.seams, front_axis=spec.front_preserve_axis,
        up_axis=plan.up_axis, fold_angle=spec.mandatory_fold_angle,
        threshold=spec.front_preserve_threshold, max_dihedral=spec.front_preserve_max_dihedral)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(block, fh, indent=2)
    print(f"[verify] interactive_constraints_passed={block['interactive_constraints_passed']} "
          f"(checked {block['checked_constraint_count']} constraint(s) over "
          f"{block['approved_chapter_count']} approved chapter(s))")
    for cname, cres in block["constraint_results"].items():
        if not cres.get("chapter_resolved", True):
            print(f"  {cname}: NOT RESOLVED in result")
            continue
        print(f"  {cname} (islands={cres['island_count']}):")
        for rule, r in cres["constraints"].items():
            if r["checkable"]:
                mark = "PASS" if r["passed"] else "FAIL"
                print(f"    [{mark}] {rule}: expected={r['expected']} actual={r['actual']}")
            else:
                print(f"    [....] {rule}: advisory ({r['note']})")
    print(f"  (NOTE: seam-level verify; overlap/distortion gate needs Blender) → {args.out}")
    return 0 if block["interactive_constraints_passed"] else 1


# --- argument parser --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="interactive_cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", default=DEFAULT_PLAN, help=f"plan JSON (default {DEFAULT_PLAN})")
    p.add_argument("--front-axis", dest="front_axis", default=None, help="e.g. +Z")
    p.add_argument("--up-axis", dest="up_axis", default=None, help="e.g. +Y")
    p.add_argument("--segmentation-mode", dest="segmentation_mode", default=None)
    p.add_argument("--forbidden-edges", dest="forbidden_edges", default=None,
                   help="comma-separated edge ids to preserve, e.g. 3054")
    p.add_argument("--object", default=None, help="asset name stored in the plan")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("observe", help="measure a chapter on the mesh")
    o.add_argument("--obj", required=True)
    o.add_argument("--chapter", required=True)
    o.add_argument("--kind", default="")
    o.add_argument("--source-parts", dest="source_parts", default="")
    o.add_argument("--source-faces", dest="source_faces", default="")
    o.set_defaults(func=cmd_observe)

    d = sub.add_parser("draft", help="propose a seam-plan draft from the observation")
    d.add_argument("--chapter", required=True)
    d.add_argument("--kind", default="")
    d.add_argument("--preferences", default="", help="JSON overrides for the template")
    d.set_defaults(func=cmd_draft)

    for name, fn, helptext in (("approve", cmd_approve, "approve a drafted chapter"),
                               ("reject", cmd_reject, "reject a chapter"),
                               ("revise", cmd_revise, "mark a chapter needs_revision")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--chapter", required=True)
        s.add_argument("--notes", default=None)
        s.set_defaults(func=fn)

    s = sub.add_parser("status", help="print the plan")
    s.set_defaults(func=cmd_status)

    e = sub.add_parser("export-guided-spec", help="write the GuidedUVSpec for approved chapters")
    e.add_argument("--out", default=DEFAULT_SPEC_OUT)
    e.add_argument("--expected-intents", dest="expected_intents", default="")
    e.set_defaults(func=cmd_export_guided_spec)

    v = sub.add_parser("verify", help="check approved constraints on the seam set (headless)")
    v.add_argument("--obj", required=True)
    v.add_argument("--out", default=DEFAULT_REPORT_OUT)
    v.set_defaults(func=cmd_verify)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
