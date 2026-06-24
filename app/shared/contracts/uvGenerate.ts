/**
 * Shared Electron <-> worker contract for the MVP 3 Generate + Optimize app.
 *
 * TypeScript mirror of `worker/app_uv_generate_contract.py` (plan §4, §5, §9).
 * Renderer and main both import from here so a schema change is a single edit
 * (plan §14 "shared contract 변경은 먼저 문서를 갱신").
 *
 * MVP 3 runs the chart engine in STRICT user/reference mode: the MVP 2
 * `active_user_seam_spec` is the source of truth and the seam set is never
 * auto-changed. Seam integrity (`auto_added_seams == 0`,
 * `final_seam_count == user_seam_count`) is a hard acceptance; a run that breaks
 * it is `needs_user_review` and must not ship (plan §1, §6).
 *
 * Rules: UV_GENERATE_SCHEMA_VERSION is pinned; new fields are optional only.
 */

export const UV_GENERATE_SCHEMA_VERSION = 1;

// --- App-facing commands (plan §4) ----------------------------------------
export const UvGenerateCommand = {
  GenerateUvFromSeams: 'generate_uv_from_seams',
  SelectUvCandidate: 'select_uv_candidate',
} as const;
export type UvGenerateCommand = (typeof UvGenerateCommand)[keyof typeof UvGenerateCommand];

// --- Run lifecycle (plan §9) — note the `needs_user_review` outcome --------
// `needs_input` is the UV-boundary-fallback revision terminal status: no active
// seam spec AND no usable UV layer, so nothing could be unwrapped (revision plan
// §1 case 3, §4.2). It is NOT a failure — the UI asks the user to pick a UV layer
// / import a UV'd model / open the Seam Editor.
export const UvGenerateRunStatus = {
  Queued: 'queued',
  Running: 'running',
  Accepted: 'accepted',
  NeedsUserReview: 'needs_user_review',
  NeedsInput: 'needs_input',
  Failed: 'failed',
  Cancelled: 'cancelled',
} as const;
export type UvGenerateRunStatus = (typeof UvGenerateRunStatus)[keyof typeof UvGenerateRunStatus];

export const UV_GENERATE_TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  UvGenerateRunStatus.Accepted,
  UvGenerateRunStatus.NeedsUserReview,
  UvGenerateRunStatus.NeedsInput,
  UvGenerateRunStatus.Failed,
  UvGenerateRunStatus.Cancelled,
]);

// --- Seam source resolution (UV-boundary-fallback revision plan §2, §4) ------
// A Generate run resolves its seam source by precedence: an explicit
// `active_user_seam_spec` wins; otherwise the selected UV layer's island
// boundaries are derived into a seam spec; otherwise the run is `needs_input`.
export const SeamSourceType = {
  UserSeamSpec: 'user_seam_spec',
  UvBoundaryDerived: 'uv_boundary_derived',
} as const;
export type SeamSourceType = (typeof SeamSourceType)[keyof typeof SeamSourceType];

/** The pre-flight seam-source kind `validateInput` reports (revision plan §4.4). */
export const SeamSourceKind = {
  Explicit: 'explicit',
  Derived: 'derived',
  Missing: 'missing',
} as const;
export type SeamSourceKind = (typeof SeamSourceKind)[keyof typeof SeamSourceKind];

export const DEFAULT_SEAM_SOURCE_POLICY = 'prefer_spec_then_uv_boundary';
export const MISSING_SEAM_SOURCE_CODE = 'missing_seam_source';
export const MISSING_SEAM_SOURCE_MESSAGE =
  'No user seam spec or usable UV layer was found. Select a UV layer or create seams.';

/** The summary's `seam_source` block (revision plan §2.3, §4). */
export interface SeamSource {
  type: SeamSourceType;
  path: string | null;
  uv_layer: string | null;
  user_confirmed: boolean;
  derived: boolean;
}

export const SUPPORTED_GENERATE_EXTS = ['.blend', '.fbx', '.obj', '.glb', '.gltf'] as const;

// --- Strict user/reference defaults (plan §1) -----------------------------
export const DEFAULT_LAYOUT_OPT_PRESET = 'user_reference';
export const DEFAULT_LAYOUT_OPT_MAX_CANDIDATES = 24;

/** The Generate options block. All strict flags default false (plan §1). */
export interface GenerateUvOptions {
  uv_engine?: string;
  auto_refine_user_seams?: boolean;
  repair_user_seams?: boolean;
  enforce_user_mandatory?: boolean;
  gate_user_mandatory?: boolean;
  optimize_layout?: boolean;
  layout_opt_preset?: string;
  layout_opt_max_candidates?: number;
  render_previews?: boolean;
  save_selected_blend?: boolean;
  // Render tuning (optional; worker defaults apply when omitted).
  texture_size_px?: number;
  checker_scale?: number;
  render_size_px?: number;
}

export const STRICT_GENERATE_OPTIONS: Required<
  Pick<
    GenerateUvOptions,
    | 'uv_engine'
    | 'auto_refine_user_seams'
    | 'repair_user_seams'
    | 'enforce_user_mandatory'
    | 'gate_user_mandatory'
    | 'optimize_layout'
    | 'layout_opt_preset'
    | 'layout_opt_max_candidates'
    | 'render_previews'
    | 'save_selected_blend'
  >
> = {
  uv_engine: 'chart',
  auto_refine_user_seams: false,
  repair_user_seams: false,
  enforce_user_mandatory: false,
  gate_user_mandatory: false,
  optimize_layout: true,
  layout_opt_preset: DEFAULT_LAYOUT_OPT_PRESET,
  layout_opt_max_candidates: DEFAULT_LAYOUT_OPT_MAX_CANDIDATES,
  render_previews: true,
  save_selected_blend: true,
};

/** Overlay caller options on the strict defaults (mirror `merge_options`). */
export function mergeGenerateOptions(user?: GenerateUvOptions | null): GenerateUvOptions {
  return { ...STRICT_GENERATE_OPTIONS, ...(user ?? {}) };
}

// --- IPC channels (preload <-> main, plan §11 Session E) ------------------
export const UvGenerateIpc = {
  ValidateInput: 'uvGenerate:validateInput',
  Start: 'uvGenerate:start',
  Cancel: 'uvGenerate:cancel',
  GetRun: 'uvGenerate:getRun',
  GetCandidateSummary: 'uvGenerate:getCandidateSummary',
} as const;

// --- Summary shapes (plan §4.1) -------------------------------------------
export interface GenerateMetrics {
  stretch_score?: number;
  worst_island_distortion?: number;
  raster_overlap_ratio?: number;
  overlap_ratio?: number;
  texel_density_variance?: number;
  packing_efficiency?: number;
  island_count?: number;
  uv_bounds_ok?: boolean;
}

export interface SeamIntegrity {
  user_seam_count: number;
  user_protected_count: number;
  final_seam_count: number;
  auto_added_seams: number;
  mandatory_rule_enabled: boolean;
  mandatory_gate_enabled: boolean;
  valid: boolean;
}

/** The honest optimization verdict (plan §2 Goal D status 문구). */
export const OptimizationVerdict = {
  Meaningful: 'meaningful',
  MinorPackingOnly: 'minor_packing_only',
  BaselineRetained: 'baseline_retained',
  NeedsBetterPacking: 'needs_better_packing',
  ConsiderSeamEdits: 'consider_seam_edits',
} as const;
export type OptimizationVerdict = (typeof OptimizationVerdict)[keyof typeof OptimizationVerdict];

/** Packing/stretch/texel deltas + meaningful flag (plan §2 Goal C). */
export interface OptimizationImprovement {
  meaningful: boolean;
  packing_delta: number;
  stretch_delta: number;
  texel_density_delta: number;
  score_ratio: number;
  packing_meaningful: boolean;
  texel_meaningful: boolean;
  score_meaningful: boolean;
}

export interface LayoutOptimizationBlock {
  enabled: boolean;
  selected_candidate_id?: string | null;
  kept_baseline?: boolean;
  candidate_count?: number;
  score_before?: number | null;
  score_after?: number | null;
  packing_efficiency_before?: number | null;
  packing_efficiency_after?: number | null;
  stretch_before?: number | null;
  stretch_after?: number | null;
  // MVP3 §2 Goal C/D: honest improvement + verdict the UI shows verbatim.
  texel_density_before?: number | null;
  texel_density_after?: number | null;
  improvement?: OptimizationImprovement;
  verdict?: OptimizationVerdict | string;
}

/** Stable artifact keys -> run-relative filenames (plan §4.1). */
export interface GenerateArtifacts {
  summary?: string;
  p5_gate?: string;
  seam_report?: string;
  candidate_summary?: string;
  // UV-boundary fallback artifacts (revision plan §3.1) — present on a derived run.
  seam_source_resolution?: string;
  derived_seam_spec?: string;
  baseline_uv_layout?: string;
  baseline_checker_front?: string;
  baseline_checker_side?: string;
  selected_uv_layout?: string;
  selected_checker_front?: string;
  selected_checker_side?: string;
  selected_blend?: string;
}

export interface UvGenerateWorkerError {
  code: string;
  message: string;
  traceback?: string;
  details?: Record<string, unknown>;
}

/** `uv_generate_summary.json` — the renderer's primary input (plan §3, §4.1). */
export interface UvGenerateSummary {
  schema_version: number;
  run_id: string;
  command: string;
  status: UvGenerateRunStatus;
  model: string | null;
  object_name: string | null;
  seam_spec: string | null;
  /** Where the seam set came from — explicit spec or derived UV boundary
   * (revision plan §2.3, §4). `null` only on legacy/pre-revision summaries. */
  seam_source: SeamSource | null;
  selected_candidate_id: string | null;
  selected_uv_model: string | null;
  metrics: GenerateMetrics;
  seam_integrity: SeamIntegrity;
  layout_optimization: LayoutOptimizationBlock;
  artifacts: GenerateArtifacts;
  warnings: string[];
}

// --- Candidate summary (plan §5) ------------------------------------------
export interface CandidateRow {
  id: string | null;
  unwrap_method: string | null;
  minimize_iters: number;
  margin: number | null;
  pack_shape: string | null;
  rotate: boolean;
  average_scale: boolean;
  // MVP3 §2 Goal B: island-level custom packing backend + pre-pass flags.
  pack_backend?: string;
  orient_long_islands?: boolean;
  density_normalize?: boolean;
  accepted: boolean;
  reason: string;
  score: number | null;
  metrics: GenerateMetrics;
}

export interface RejectedCandidate {
  id: string | null;
  reason: string;
}

export interface CandidateSummary {
  schema_version: number;
  baseline_candidate_id: string | null;
  selected_candidate_id: string | null;
  kept_baseline: boolean;
  score_weights: Record<string, number>;
  candidates: CandidateRow[];
  rejected: RejectedCandidate[];
}

export interface UvGenerateStatusDoc {
  schema_version: number;
  run_id: string;
  command: string;
  status: UvGenerateRunStatus;
  started_at: string;
  finished_at: string | null;
  input: Record<string, unknown>;
  artifacts: Record<string, string>;
  error: UvGenerateWorkerError | null;
}

/** Combined generate-run view the renderer reads via `uvGenerate:getRun`. */
export interface UvGenerateRunView {
  run_id: string;
  dir: string;
  status: UvGenerateStatusDoc | null;
  summary: UvGenerateSummary | null;
  candidate_summary: CandidateSummary | null;
  /** Parsed `p5_gate.json` for the debug tab (plan §5 "Raw p5_gate.json은 debug tab"). */
  p5_gate: Record<string, unknown> | null;
  /** Parsed `seam_report.json` for the raw-report tab. */
  seam_report: Record<string, unknown> | null;
  stdout: string;
  stderr: string;
  /** Stable artifact key -> absolute path on disk, for `uvpreview://` rendering. */
  artifact_paths: Record<string, string>;
}

// --- validateInput (plan §8 "Validate Seam Spec" pre-flight) --------------
export interface ValidateGenerateIssue {
  code: string;
  message: string;
}

/** Pre-flight readiness for a Generate run (cheap, pure-Node checks). */
export interface ValidateGenerateInput {
  ready: boolean;
  model: string | null;
  object_name: string | null;
  seam_spec: string | null;
  spec_object: string | null;
  user_seam_count: number | null;
  user_protected_count: number | null;
  object_mismatch: boolean;
  /** Which seam source the run will use (revision plan §4.4, §4.5). `derived`
   * means no usable spec but a selected UV layer exists; `missing` blocks Generate. */
  seam_source: SeamSourceKind;
  /** The selected/active UV layer used for the derived fallback (revision plan §4.4). */
  selected_uv_layer: string | null;
  issues: ValidateGenerateIssue[];
}

export interface StartGenerateResult {
  run_id: string;
}

/** select_uv_candidate placeholder result (plan §4.2 — read-only in MVP 3.0). */
export interface SelectCandidateResult {
  status: string;
  selected_candidate_id: string;
  selected_uv_model: string | null;
}
