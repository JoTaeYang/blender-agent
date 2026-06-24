/**
 * Shared Electron <-> worker contract for the MVP 5 Production Export app.
 *
 * TypeScript mirror of `worker/app_export_contract.py` (plan §4, §5, §6, §7, §8, §9).
 * Renderer and main both import from here so a schema change is a single edit
 * (plan §16 "shared contract 변경은 먼저 문서를 갱신").
 *
 * MVP 5 exports the MVP 3 accepted `selected_uv_model` to FBX/OBJ/GLB/GLTF with
 * validation, writes an export manifest tied to the source UV run, appends an
 * append-only project history, and supports rollback to a previous UV run or
 * export. MVP 4 AI Review is skipped and is informational only (plan §0).
 *
 * Rules: EXPORT_SCHEMA_VERSION is pinned; new fields are optional only.
 */

export const EXPORT_SCHEMA_VERSION = 1;

// --- App-facing commands (plan §4, §5, §9) --------------------------------
export const ExportCommand = {
  CheckExportReadiness: 'check_export_readiness',
  ExportProductionAsset: 'export_production_asset',
  ListRollbackTargets: 'list_rollback_targets',
  RollbackProjectState: 'rollback_project_state',
} as const;
export type ExportCommand = (typeof ExportCommand)[keyof typeof ExportCommand];

// --- Export run lifecycle (plan §5) — note the `partial` outcome ----------
export const ExportRunStatus = {
  Queued: 'queued',
  Running: 'running',
  Accepted: 'accepted',
  Partial: 'partial',
  Failed: 'failed',
  Cancelled: 'cancelled',
} as const;
export type ExportRunStatus = (typeof ExportRunStatus)[keyof typeof ExportRunStatus];

export const EXPORT_TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  ExportRunStatus.Accepted,
  ExportRunStatus.Partial,
  ExportRunStatus.Failed,
  ExportRunStatus.Cancelled,
]);
/** An export that produced a manifest the app pins as `latest_export_id` (plan §14). */
export const EXPORT_SHIPPED_STATUSES: ReadonlySet<string> = new Set([
  ExportRunStatus.Accepted,
  ExportRunStatus.Partial,
]);

// --- Readiness status (plan §4.1) -----------------------------------------
export const ExportReadinessStatus = {
  Accepted: 'accepted',
  NeedsInput: 'needs_input',
} as const;
export type ExportReadinessStatus =
  (typeof ExportReadinessStatus)[keyof typeof ExportReadinessStatus];

// --- Supported formats (plan §5.1) ----------------------------------------
export const ExportFormat = {
  Fbx: 'fbx',
  Obj: 'obj',
  Glb: 'glb',
  Gltf: 'gltf',
} as const;
export type ExportFormat = (typeof ExportFormat)[keyof typeof ExportFormat];
export const SUPPORTED_EXPORT_FORMATS = ['fbx', 'obj', 'glb', 'gltf'] as const;
export const EXPORT_FORMAT_EXT: Record<string, string> = {
  fbx: '.fbx',
  obj: '.obj',
  glb: '.glb',
  gltf: '.gltf',
};

// --- History event + rollback target types (plan §8, §9) ------------------
export const ExportEventType = {
  UvSelected: 'uv_selected',
  ExportCreated: 'export_created',
  ExportFailed: 'export_failed',
  RollbackPerformed: 'rollback_performed',
} as const;
export type ExportEventType = (typeof ExportEventType)[keyof typeof ExportEventType];

export const RollbackTargetType = {
  UvRun: 'uv_run',
  Export: 'export',
} as const;
export type RollbackTargetType = (typeof RollbackTargetType)[keyof typeof RollbackTargetType];

// The UV layer the MVP 3 Generate + Optimize run writes the OPTIMIZED layout onto
// (mirror of `uv_agent.blender.organic_unwrap.AI_UV_LAYER`). This — NOT the original
// review layer (`project.selected_uv_layer`, e.g. `UVChannel_1`) — is what export must
// ship. The OBJ/glTF exporters write the `active_render` layer; the worker activates this
// one and marks it for render. Defaulting the export UV-layer field to the original review
// layer was the "exported OBJ ≠ preview" bug (it shipped the un-optimized UVs).
export const GENERATED_UV_LAYER = 'AI_UV';

// --- Export options (plan §5.1 options block) -----------------------------
export interface ExportOptions {
  selected_uv_layer?: string | null;
  apply_scale?: boolean;
  include_materials?: boolean;
  include_normals?: boolean;
  copy_textures?: boolean;
  triangulate?: boolean;
  axis_forward?: string;
  axis_up?: string;
  export_name?: string | null;
  // Render tuning (optional; worker defaults apply when omitted).
  render_previews?: boolean;
  texture_size_px?: number;
  checker_scale?: number;
  render_size_px?: number;
}

export const DEFAULT_EXPORT_OPTIONS: Required<
  Pick<
    ExportOptions,
    | 'selected_uv_layer'
    | 'apply_scale'
    | 'include_materials'
    | 'include_normals'
    | 'copy_textures'
    | 'triangulate'
    | 'axis_forward'
    | 'axis_up'
    | 'export_name'
  >
> = {
  selected_uv_layer: null,
  apply_scale: true,
  include_materials: true,
  include_normals: true,
  copy_textures: false,
  triangulate: false,
  axis_forward: '-Z',
  axis_up: 'Y',
  export_name: null,
};

/** Overlay caller options on the export defaults (mirror `merge_options`). */
export function mergeExportOptions(user?: ExportOptions | null): ExportOptions {
  return { ...DEFAULT_EXPORT_OPTIONS, ...(user ?? {}) };
}

// --- IPC channels (preload <-> main, plan §12 Session E) ------------------
export const ExportIpc = {
  CheckReadiness: 'export:checkReadiness',
  Start: 'export:start',
  Cancel: 'export:cancel',
  GetRun: 'export:getRun',
  ListHistory: 'export:listHistory',
  ListRollbackTargets: 'export:listRollbackTargets',
  Rollback: 'export:rollback',
  RevealFile: 'export:revealFile',
} as const;

// --- Readiness (plan §4.1) ------------------------------------------------
export interface ExportReadinessChecks {
  model_exists: boolean;
  summary_exists: boolean;
  uv_run_accepted: boolean;
  raster_overlap_ok: boolean;
  uv_bounds_ok: boolean;
  seam_integrity_ok: boolean;
  ai_review_required: boolean;
  ai_review_skipped: boolean;
}

export interface ExportBlockingIssue {
  code: string;
  message: string;
}

export interface ExportReadiness {
  schema_version: number;
  status: ExportReadinessStatus;
  ready: boolean;
  selected_uv_model: string | null;
  source_uv_run_id: string | null;
  checks: Partial<ExportReadinessChecks>;
  blocking_issues: ExportBlockingIssue[];
  warnings: string[];
}

// --- Source + metric blocks (plan §5.1, §6) -------------------------------
export interface ExportResultSource {
  selected_uv_model: string | null;
  selected_uv_summary: string | null;
  uv_generate_run_id: string | null;
  seam_spec: string | null;
  selected_candidate_id: string | null;
  ai_review_run_id: string | null;
  ai_review_skipped: boolean;
}

export interface ExportManifestSource {
  selected_uv_model: string | null;
  selected_uv_summary: string | null;
  uv_generate_run_id: string | null;
  active_user_seam_spec: string | null;
  candidate_summary: string | null;
  p5_gate: string | null;
  seam_report: string | null;
  ai_review_run_id: string | null;
  ai_review_skipped: boolean;
}

export interface ExportMetrics {
  stretch_score?: number;
  worst_island_distortion?: number;
  raster_overlap_ratio?: number;
  texel_density_variance?: number;
  packing_efficiency?: number;
}

// --- Validation report (plan §7) ------------------------------------------
export interface FormatValidation {
  reopen_ok: boolean;
  mesh_count: number;
  faces: number;
  vertices: number;
  uv_layers: string[];
  has_uv: boolean;
  has_normals: boolean;
  warnings: string[];
}

export interface ValidationReport {
  schema_version: number;
  status: ExportRunStatus;
  formats: Record<string, FormatValidation>;
}

// --- Export manifest (plan §6 — source of truth for history UI) -----------
export interface ExportManifest {
  schema_version: number;
  export_id: string;
  created_at: string;
  status: ExportRunStatus;
  formats: string[];
  options: Pick<
    Required<ExportOptions>,
    | 'selected_uv_layer'
    | 'apply_scale'
    | 'include_materials'
    | 'include_normals'
    | 'copy_textures'
    | 'triangulate'
  >;
  source: ExportManifestSource;
  metrics: ExportMetrics;
  files: Record<string, string>; // format/preview key -> bare filename
  validation: string;
}

export interface ExportWorkerError {
  code: string;
  message: string;
  traceback?: string;
  failed_formats?: FailedFormat[];
  details?: Record<string, unknown>;
}

export interface FailedFormat {
  format: string;
  code: string;
  message: string;
}

/** The `export_production_asset` result (plan §5.1). */
export interface ExportResult {
  schema_version: number;
  export_id: string;
  command: string;
  status: ExportRunStatus;
  source: ExportResultSource;
  exports: Record<string, string>; // succeeded format -> project-relative path
  validation: ValidationReport;
  artifacts: Record<string, string>;
  failed_formats?: FailedFormat[];
  warnings: string[];
}

export interface ExportStatusDoc {
  schema_version: number;
  export_id: string;
  command: string;
  status: ExportRunStatus;
  started_at: string;
  finished_at: string | null;
  input: Record<string, unknown>;
  artifacts: Record<string, string>;
  error: ExportWorkerError | null;
}

/** Combined export-run view the renderer reads via `export:getRun`. */
export interface ExportRunView {
  export_id: string;
  dir: string;
  status: ExportStatusDoc | null;
  manifest: ExportManifest | null;
  validation: ValidationReport | null;
  result: ExportResult | null;
  stdout: string;
  stderr: string;
  /** Stable artifact key -> absolute path on disk, for `uvpreview://` rendering. */
  artifact_paths: Record<string, string>;
  /** Format/preview key -> absolute path of the exported file, for reveal-in-folder. */
  file_paths: Record<string, string>;
}

// --- Project history (plan §8) --------------------------------------------
export interface ExportEventSummary {
  formats: string[];
  status: ExportRunStatus;
  raster_overlap_ratio?: number;
  packing_efficiency?: number;
}

export interface HistoryEvent {
  id: string;
  type: ExportEventType;
  created_at: string;
  // export_created / export_failed
  export_id?: string;
  uv_generate_run_id?: string | null;
  selected_candidate_id?: string | null;
  seam_spec?: string | null;
  manifest?: string;
  summary?: ExportEventSummary;
  // rollback_performed
  target_type?: RollbackTargetType;
  target_id?: string;
  selected_uv_model?: string | null;
  selected_uv_summary?: string | null;
}

export interface ProjectHistory {
  schema_version: number;
  events: HistoryEvent[];
}

// --- Rollback (plan §9) ---------------------------------------------------
export interface RollbackTarget {
  id: string;
  type: RollbackTargetType;
  created_at: string | null;
  // uv_run
  selected_uv_model?: string | null;
  selected_candidate_id?: string | null;
  metrics?: ExportMetrics;
  // export
  manifest?: string;
  formats?: string[];
  status?: ExportRunStatus;
}

export interface ListRollbackTargetsResult {
  schema_version: number;
  status: string;
  targets: RollbackTarget[];
}

export interface RollbackResult {
  schema_version: number;
  status: string;
  rolled_back_to: {
    type: RollbackTargetType;
    id: string;
    selected_uv_model?: string | null;
    selected_uv_summary?: string | null;
    latest_export_id?: string | null;
  };
  history_event: string;
}

export interface StartExportResult {
  export_id: string;
}
