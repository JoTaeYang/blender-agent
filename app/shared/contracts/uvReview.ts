/**
 * Shared Electron <-> worker contract for the MVP 1 UV Review app.
 *
 * TypeScript mirror of `worker/app_uv_review_contract.py`. Renderer and main both
 * import from here so a schema change is a single edit (plan §14 contract drift).
 *
 * MVP 1 is read-only review: nothing here mutates UVs, and the mandatory-90 rule
 * is deliberately absent (plan §6).
 *
 * Rules: SCHEMA_VERSION is pinned; new fields are optional only.
 */

export const UV_SCHEMA_VERSION = 1;

// --- App-facing UV review commands (plan §5) ------------------------------
export const UvCommand = {
  InspectUvLayers: 'inspect_uv_layers',
  ReviewExistingUv: 'review_existing_uv',
  SetActiveUvLayer: 'set_active_uv_layer',
} as const;
export type UvCommand = (typeof UvCommand)[keyof typeof UvCommand];

// --- Review run lifecycle (plan §9) — note the MVP-1 `no_uv` outcome -------
export const UvRunStatus = {
  Queued: 'queued',
  Running: 'running',
  Accepted: 'accepted',
  NoUv: 'no_uv',
  Failed: 'failed',
  Cancelled: 'cancelled',
} as const;
export type UvRunStatus = (typeof UvRunStatus)[keyof typeof UvRunStatus];

export const UV_TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  UvRunStatus.Accepted,
  UvRunStatus.NoUv,
  UvRunStatus.Failed,
  UvRunStatus.Cancelled,
]);

// --- Review status (plan §6) ----------------------------------------------
export const ReviewStatus = {
  Clean: 'clean',
  HasOverlap: 'has_overlap',
  HighStretch: 'high_stretch',
  DensityVariance: 'density_variance',
  OutOfBounds: 'out_of_bounds',
  NoUv: 'no_uv',
  Unknown: 'unknown',
} as const;
export type ReviewStatus = (typeof ReviewStatus)[keyof typeof ReviewStatus];

export const SUPPORTED_REVIEW_EXTS = ['.blend', '.fbx', '.obj', '.glb', '.gltf'] as const;

// --- IPC channels (preload <-> main, plan §5 IPC API) ---------------------
export const UvIpc = {
  InspectLayers: 'uv:inspectLayers',
  SetActiveLayer: 'uv:setActiveLayer',
  ReviewExisting: 'uv:reviewExisting',
  GetReviewRun: 'uv:getReviewRun',
} as const;

// --- inspect_uv_layers output (plan §5.1) ---------------------------------
export interface UvLayerInfo {
  name: string;
  active: boolean;
  loop_count: number;
  empty: boolean;
}

export interface UvObjectSummary {
  name: string;
  vertices: number;
  edges: number;
  faces: number;
  materials: string[];
  uv_layers: UvLayerInfo[];
  active_uv_layer: string | null;
  has_uv: boolean;
}

export interface InspectUvResult {
  schema_version: number;
  status: 'accepted' | 'no_uv' | 'failed';
  command: string;
  project_id?: string | null;
  model?: string;
  objects?: UvObjectSummary[];
  recommended_next_step?: 'review_existing_uv' | 'open_seam_editor_or_generate_uv';
  warnings?: string[];
  error?: UvWorkerError;
}

// --- review_existing_uv output (plan §5.2, §6) ----------------------------
export interface ReviewOptions {
  texture_size_px?: number;
  checker_scale?: number;
  render_size_px?: number;
  raster_overlap_resolution?: number;
  make_heatmaps?: boolean;
  make_3q?: boolean;
}

export interface UvBounds {
  min: [number, number];
  max: [number, number];
  in_0_1: boolean;
}

export interface UvMetrics {
  stretch_score: number;
  worst_island_distortion: number;
  overlap_ratio: number;
  raster_overlap_ratio: number;
  self_overlap_ratio: number;
  cross_overlap_ratio: number;
  texel_density_variance: number;
  packing_efficiency: number;
}

export interface UvSummaryBlock {
  island_count: number;
  uv_bounds: UvBounds;
  has_negative_uv: boolean;
  has_out_of_tile_uv: boolean;
}

export interface MeshSummaryBlock {
  vertices: number;
  edges: number;
  faces: number;
  loops: number;
}

export interface ReviewIssue {
  code: string;
  severity: 'error' | 'warning';
  message: string;
  metric: string | null;
  value?: number;
}

/** Stable artifact keys -> run-relative filenames (plan §7). */
export interface UvArtifacts {
  summary?: string;
  metrics?: string;
  uv_layers?: string;
  uv_bounds?: string;
  uv_layout?: string;
  uv_layout_svg?: string;
  checker_front?: string;
  checker_side?: string;
  checker_3q?: string | null;
  overlap_mask?: string | null;
  stretch_heatmap?: string | null;
}

/** `uv_review_summary.json` — the renderer's primary input (plan §9). */
export interface UvReviewSummary {
  schema_version: number;
  run_id: string;
  command: string;
  status: 'accepted' | 'no_uv';
  model: string | null;
  object_name: string | null;
  uv_layer: string | null;
  mesh: MeshSummaryBlock | null;
  uv: UvSummaryBlock | null;
  metrics: UvMetrics | null;
  review_status: ReviewStatus;
  issues: ReviewIssue[];
  artifacts: UvArtifacts;
  warnings: string[];
}

export interface UvWorkerError {
  code: string;
  message: string;
  traceback?: string;
}

export interface UvReviewStatusDoc {
  schema_version: number;
  run_id: string;
  command: string;
  status: UvRunStatus;
  started_at: string;
  finished_at: string | null;
  input: Record<string, unknown>;
  artifacts: Record<string, string>;
  error: UvWorkerError | null;
}

/** Combined review-run view the renderer reads via `uv:getReviewRun` (plan §9). */
export interface UvReviewRunView {
  run_id: string;
  dir: string;
  status: UvReviewStatusDoc | null;
  summary: UvReviewSummary | null;
  stdout: string;
  stderr: string;
  /** Stable artifact key -> absolute path on disk, for `uvpreview://` rendering. */
  artifact_paths: Record<string, string>;
}
