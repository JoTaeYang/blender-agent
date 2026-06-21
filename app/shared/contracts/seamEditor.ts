/**
 * Shared Electron <-> worker contract for the MVP 2 Seam Spec Editor.
 *
 * TypeScript mirror of `worker/app_seam_spec_contract.py` (plan §4, §5, §6, §9).
 * Renderer and main both import from here so a schema change is a single edit
 * (plan §14 "shared contract 변경은 먼저 문서를 갱신").
 *
 * MVP 2 is non-generative: nothing here unwraps/packs UVs or auto-adds the
 * mandatory-90 fold; the user's seam/protect choices are the source of truth
 * (plan §1, §13). `normalizeAndValidateSpec` re-implements the Python rules so
 * the app can validate/save a spec without spawning Blender (plan §4, §6.2, §6.3).
 *
 * Rules: SEAM_SCHEMA_VERSION is pinned; new fields are optional only.
 */

export const SEAM_SCHEMA_VERSION = 1;
export const DEFAULT_FOLD_ANGLE = 90.0;
export const SPEC_MODE = 'user_seams';

// --- App-facing seam-editor commands (plan §5, §6) ------------------------
export const SeamCommand = {
  ExportEdgeGeometry: 'export_edge_geometry',
  LoadUserSeamSpec: 'load_user_seam_spec',
  SaveUserSeamSpec: 'save_user_seam_spec',
  ValidateUserSeamSpec: 'validate_user_seam_spec',
  ExtractUvBoundary: 'extract_uv_boundary_as_seams',
} as const;
export type SeamCommand = (typeof SeamCommand)[keyof typeof SeamCommand];

// --- Run lifecycle (plan §9) — note the `no_uv` outcome for extract --------
export const SeamRunStatus = {
  Queued: 'queued',
  Running: 'running',
  Accepted: 'accepted',
  NoUv: 'no_uv',
  Failed: 'failed',
  Cancelled: 'cancelled',
} as const;
export type SeamRunStatus = (typeof SeamRunStatus)[keyof typeof SeamRunStatus];

export const SEAM_TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  SeamRunStatus.Accepted,
  SeamRunStatus.NoUv,
  SeamRunStatus.Failed,
  SeamRunStatus.Cancelled,
]);

// --- Edge states in the editor (plan §7) ----------------------------------
export const EdgeState = {
  Normal: 'normal',
  Seam: 'seam',
  Protected: 'protected',
  Selected: 'selected',
  Hovered: 'hovered',
  Invalid: 'invalid',
  Conflict: 'conflict',
} as const;
export type EdgeState = (typeof EdgeState)[keyof typeof EdgeState];

export const CONFLICT_SEAM_AND_PROTECTED = 'seam_and_protected';
export const RESOLUTION_SEAM_WINS = 'seam_wins';

// --- IPC channels (preload <-> main, plan §11 Session D) ------------------
export const SeamIpc = {
  ExportEdgeGeometry: 'seam:exportEdgeGeometry',
  LoadSpec: 'seam:loadSpec',
  SaveSpec: 'seam:saveSpec',
  ValidateSpec: 'seam:validateSpec',
  ExtractUvBoundary: 'seam:extractUvBoundary',
  GetEditorRun: 'seam:getEditorRun',
} as const;

// --- edge_geometry.json (plan §5.1) ---------------------------------------
export interface EdgeGeometryVertex {
  id: number;
  co: [number, number, number];
}
export interface EdgeGeometryEdge {
  id: number;
  vertex_ids: [number, number];
  face_ids: number[];
  is_boundary: boolean;
  is_non_manifold: boolean;
  is_sharp: boolean;
  is_seam: boolean;
  dihedral_angle: number;
}
export interface EdgeGeometryFace {
  id: number;
  vertex_ids: number[];
  edge_ids: number[];
  material_index: number;
}
export interface EdgeGeometry {
  schema_version: number;
  object: string;
  vertices: EdgeGeometryVertex[];
  edges: EdgeGeometryEdge[];
  faces: EdgeGeometryFace[];
}

export interface MeshSignature {
  vertices: number;
  edges: number;
  faces: number;
  loops: number;
}

// --- Canonical user_seam_spec.json (plan §4) — UserSeamSpec schema ---------
export interface SeamSpec {
  version: number;
  object: string;
  mode: string;
  mandatory_fold_angle: number;
  user_seam_edges: number[];
  user_protected_edges: number[];
  chapters: unknown[];
  notes: string;
}

export interface SeamConflict {
  edge_id: number;
  type: string;
  resolution: string;
}

/** Full normalization report — superset of every command's `validation` block. */
export interface SeamValidation {
  valid: boolean;
  object_mismatch: boolean;
  invalid_edges: number[];
  conflicts: SeamConflict[];
  normalized_spec: SeamSpec;
  user_seam_count: number;
  user_protected_count: number;
}

export interface SeamWorkerError {
  code: string;
  message: string;
  traceback?: string;
}

// --- Command results (plan §5.1, §6.1–§6.4) -------------------------------
export interface ExportEdgeGeometryResult {
  schema_version: number;
  status: SeamRunStatus;
  command: string;
  object_name?: string;
  mesh_signature?: MeshSignature;
  artifacts?: Record<string, string>;
  warnings?: string[];
  error?: SeamWorkerError;
}

export interface UvBoundaryReport {
  boundary_edge_count: number;
  mesh_boundary_edges: number[];
  ambiguous_edges: number[];
  non_manifold_edges: number[];
  uv_layer_missing: boolean;
}

export interface ExtractUvBoundaryResult {
  schema_version: number;
  status: SeamRunStatus;
  command: string;
  path: string | null;
  object_name?: string;
  uv_layer?: string;
  user_seam_count?: number;
  user_protected_count?: number;
  spec?: SeamSpec;
  report?: UvBoundaryReport;
  warnings?: string[];
  error?: SeamWorkerError;
}

export interface SeamEditorStatusDoc {
  schema_version: number;
  run_id: string;
  command: string;
  status: SeamRunStatus;
  started_at: string;
  finished_at: string | null;
  input: Record<string, unknown>;
  artifacts: Record<string, string>;
  error: SeamWorkerError | null;
}

/** Combined editor-run view the renderer reads via `seam:getEditorRun`. */
export interface SeamEditorRunView {
  run_id: string;
  dir: string;
  status: SeamEditorStatusDoc | null;
  /** Parsed `edge_geometry.json` when this run was an edge-geometry export. */
  edge_geometry: EdgeGeometry | null;
  /** Parsed `export_result.json` (mesh signature + artifacts) for an export. */
  export_result: ExportEdgeGeometryResult | null;
  /** Parsed `boundary_extract_report.json` for a UV-boundary extraction. */
  boundary: ExtractUvBoundaryResult | null;
  stdout: string;
  stderr: string;
}

/** UI-only editor state, persisted separately from the canonical spec (plan §4). */
export interface SeamEditorState {
  schema_version: number;
  object: string;
  selected_edges: number[];
  hidden_edges: number[];
  view: { camera: unknown | null; overlay_mode: string };
  draft_source: 'manual' | 'uv_boundary' | 'loaded_spec';
  last_saved_spec: string | null;
}

// ===========================================================================
// Pure helpers (mirror app_seam_spec_contract.py) — usable in main + renderer
// ===========================================================================
function intSet(values: Iterable<unknown> | undefined): Set<number> {
  const out = new Set<number>();
  for (const v of values ?? []) {
    const n = typeof v === 'number' ? v : parseInt(String(v), 10);
    if (Number.isInteger(n)) out.add(n);
  }
  return out;
}

const asc = (a: number, b: number) => a - b;

/** Build a canonical spec dict (UserSeamSpec schema, sorted ids) — plan §4. */
export function makeSeamSpec(input: {
  object: string;
  user_seam_edges?: Iterable<number>;
  user_protected_edges?: Iterable<number>;
  mandatory_fold_angle?: number;
  chapters?: unknown[];
  notes?: string;
}): SeamSpec {
  return {
    version: SEAM_SCHEMA_VERSION,
    object: input.object,
    mode: SPEC_MODE,
    mandatory_fold_angle: input.mandatory_fold_angle ?? DEFAULT_FOLD_ANGLE,
    user_seam_edges: [...intSet(input.user_seam_edges)].sort(asc),
    user_protected_edges: [...intSet(input.user_protected_edges)].sort(asc),
    chapters: input.chapters ?? [],
    notes: input.notes ?? '',
  };
}

/** Loose spec shape accepted by {@link normalizeAndValidateSpec} (a canonical
 * `SeamSpec` or any partially-authored dict from the editor / a JSON file). */
export type SpecInput = {
  version?: unknown;
  object?: unknown;
  mode?: unknown;
  mandatory_fold_angle?: unknown;
  user_seam_edges?: unknown;
  user_protected_edges?: unknown;
  chapters?: unknown;
  notes?: unknown;
};

/**
 * Validate + normalize a spec against a mesh (plan §6.3). Mirrors the Python
 * `normalize_and_validate_spec` exactly so the app and the worker agree:
 *
 * - out-of-range edge ids (when `edgeCount` is known) -> `invalid_edges`, dropped;
 * - an edge marked both seam and protected -> `conflicts` (seam wins, removed
 *   from protected);
 * - `objectName` differing from the spec's object -> `object_mismatch`;
 * - `valid` is true only when clean (no invalid, no conflicts, no mismatch).
 *
 * `chapters` pass through untouched (the editor never authors them, plan §4).
 */
export function normalizeAndValidateSpec(
  spec: SpecInput,
  opts: { edgeCount?: number | null; objectName?: string | null } = {},
): SeamValidation {
  const { edgeCount = null, objectName = null } = opts;
  const specObject = String(spec.object ?? '');
  const rawSeams = intSet(spec.user_seam_edges as Iterable<unknown> | undefined);
  const rawProtected = intSet(spec.user_protected_edges as Iterable<unknown> | undefined);

  const inRange = (e: number) => edgeCount === null || edgeCount === undefined || (e >= 0 && e < edgeCount);

  const invalid = [...new Set([...rawSeams, ...rawProtected])].filter((e) => !inRange(e)).sort(asc);
  const seams = new Set([...rawSeams].filter(inRange));
  const protectedSet = new Set([...rawProtected].filter(inRange));

  // seam wins: an edge marked both seam and protected ships as a seam (plan §4).
  const conflictIds = [...seams].filter((e) => protectedSet.has(e)).sort(asc);
  const conflicts: SeamConflict[] = conflictIds.map((edge_id) => ({
    edge_id,
    type: CONFLICT_SEAM_AND_PROTECTED,
    resolution: RESOLUTION_SEAM_WINS,
  }));
  for (const e of conflictIds) protectedSet.delete(e);

  const objectMismatch = !!(objectName && specObject && specObject !== objectName);

  const normalized = makeSeamSpec({
    object: specObject,
    user_seam_edges: seams,
    user_protected_edges: protectedSet,
    mandatory_fold_angle:
      typeof spec.mandatory_fold_angle === 'number' ? spec.mandatory_fold_angle : DEFAULT_FOLD_ANGLE,
    chapters: (spec.chapters as unknown[]) ?? [],
    notes: typeof spec.notes === 'string' ? spec.notes : '',
  });

  return {
    valid: invalid.length === 0 && conflicts.length === 0 && !objectMismatch,
    object_mismatch: objectMismatch,
    invalid_edges: invalid,
    conflicts,
    normalized_spec: normalized,
    user_seam_count: seams.size,
    user_protected_count: protectedSet.size,
  };
}
