/**
 * Pure seam-editing helpers (plan §7 Renderer Editing Contract).
 *
 * The editor's source of truth is two id sets — `user_seam_edges` and
 * `user_protected_edges`. These functions apply the plan §7 state transitions to
 * a selection and derive each edge's display state for the viewport. Kept pure
 * (no React, no Three.js) so the transition rules are obvious and testable.
 */

import { EdgeState, type SeamSpec } from '@shared/contracts';

/** The two authoritative id sets the editor mutates. */
export interface SeamSets {
  seams: Set<number>;
  protectedEdges: Set<number>;
}

function clone(sets: SeamSets): SeamSets {
  return { seams: new Set(sets.seams), protectedEdges: new Set(sets.protectedEdges) };
}

/** `normal/protected -> seam` for each selected edge (plan §7). */
export function markSeam(sets: SeamSets, selected: Iterable<number>): SeamSets {
  const next = clone(sets);
  for (const e of selected) {
    next.seams.add(e);
    next.protectedEdges.delete(e);
  }
  return next;
}

/** `normal/seam -> protected` for each selected edge (plan §7). */
export function markProtected(sets: SeamSets, selected: Iterable<number>): SeamSets {
  const next = clone(sets);
  for (const e of selected) {
    next.protectedEdges.add(e);
    next.seams.delete(e);
  }
  return next;
}

/** `seam/protected -> normal` for each selected edge (plan §7). */
export function clearEdges(sets: SeamSets, selected: Iterable<number>): SeamSets {
  const next = clone(sets);
  for (const e of selected) {
    next.seams.delete(e);
    next.protectedEdges.delete(e);
  }
  return next;
}

/** Context for {@link deriveEdgeState}. */
export interface EdgeStateContext {
  seams: Set<number>;
  protectedEdges: Set<number>;
  selected: Set<number>;
  hovered: number | null;
  invalid: Set<number>;
  conflict: Set<number>;
  draft: Set<number>;
}

/**
 * The single display state for an edge, highest priority first (plan §7/§8):
 * hovered > selected > conflict > invalid > seam > protected > draft > normal.
 * Selection is kept visually distinct from seam/protect (plan §8 visual rules).
 */
export function deriveEdgeState(id: number, ctx: EdgeStateContext): EdgeState {
  if (ctx.hovered === id) return EdgeState.Hovered;
  if (ctx.selected.has(id)) return EdgeState.Selected;
  if (ctx.conflict.has(id)) return EdgeState.Conflict;
  if (ctx.invalid.has(id)) return EdgeState.Invalid;
  if (ctx.seams.has(id)) return EdgeState.Seam;
  if (ctx.protectedEdges.has(id)) return EdgeState.Protected;
  if (ctx.draft.has(id)) return EdgeState.Normal; // draft drawn separately as overlay
  return EdgeState.Normal;
}

/** Edges that are both seam and protected — surfaced as conflicts (plan §4/§7). */
export function conflictEdges(sets: SeamSets): Set<number> {
  const out = new Set<number>();
  for (const e of sets.seams) if (sets.protectedEdges.has(e)) out.add(e);
  return out;
}

/** Edge ids outside `[0, edgeCount)` among the two sets (plan §4 invalid edges). */
export function invalidEdges(sets: SeamSets, edgeCount: number | null): Set<number> {
  const out = new Set<number>();
  if (edgeCount === null) return out;
  for (const e of sets.seams) if (e < 0 || e >= edgeCount) out.add(e);
  for (const e of sets.protectedEdges) if (e < 0 || e >= edgeCount) out.add(e);
  return out;
}

/** Build the canonical spec the app saves from the current editor sets (plan §4). */
export function specFromSets(
  objectName: string,
  sets: SeamSets,
  opts: { mandatoryFoldAngle?: number; notes?: string } = {},
): SeamSpec {
  return {
    version: 1,
    object: objectName,
    mode: 'user_seams',
    mandatory_fold_angle: opts.mandatoryFoldAngle ?? 90.0,
    user_seam_edges: [...sets.seams].sort((a, b) => a - b),
    user_protected_edges: [...sets.protectedEdges].sort((a, b) => a - b),
    chapters: [],
    notes: opts.notes ?? '',
  };
}

/** Load a spec's id sets into the editor (plan §7 "loaded spec이 overlay에 반영"). */
export function setsFromSpec(spec: SeamSpec): SeamSets {
  return {
    seams: new Set(spec.user_seam_edges ?? []),
    protectedEdges: new Set(spec.user_protected_edges ?? []),
  };
}
