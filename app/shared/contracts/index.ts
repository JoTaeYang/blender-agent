/**
 * Shared Electron <-> worker contract for the MVP 0 review app.
 *
 * TypeScript mirror of `worker/app_job_contract.py`. Renderer and main both import
 * from here so a schema change is a single edit (plan §10 "contract drift").
 *
 * Rules: SCHEMA_VERSION is pinned; new fields are optional only.
 */

export const SCHEMA_VERSION = 1;

// --- App-facing worker commands (plan §5) ---------------------------------
export const Command = {
  InspectModel: 'inspect_model',
  GenerateLowpoly: 'generate_lowpoly',
  ApproveLowpoly: 'approve_lowpoly',
} as const;
export type Command = (typeof Command)[keyof typeof Command];

// --- Run status lifecycle (plan §4) ---------------------------------------
export const RunStatus = {
  Queued: 'queued',
  Running: 'running',
  Accepted: 'accepted',
  Rejected: 'rejected',
  Failed: 'failed',
  Cancelled: 'cancelled',
} as const;
export type RunStatus = (typeof RunStatus)[keyof typeof RunStatus];

export const SUPPORTED_IMPORT_EXTS = ['.fbx', '.obj', '.glb', '.gltf'] as const;

// --- Mesh role hints (plan §5.1) ------------------------------------------
export type MeshRole = 'lowpoly' | 'highpoly' | 'unknown';
export type NextStep =
  | 'approve_existing_lowpoly'
  | 'generate_lowpoly'
  | 'inspect_manually';

// --- IPC channel names (preload <-> main) ---------------------------------
export const Ipc = {
  ProjectCreate: 'project:create',
  ProjectOpen: 'project:open',
  ProjectGet: 'project:get',
  ModelInspect: 'model:inspect',
  LowpolyGenerate: 'lowpoly:generate',
  LowpolyApprove: 'lowpoly:approve',
  RunGet: 'run:get',
  RunList: 'run:list',
  SettingsGet: 'settings:get',
  SettingsSet: 'settings:set',
  PickFile: 'dialog:pickFile',
  PickProjectDir: 'dialog:pickProjectDir',
  // main -> renderer push event
  RunUpdate: 'run:update',
} as const;

// --- Project + run documents (plan §4) ------------------------------------
export interface Project {
  schema_version: number;
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  dir?: string; // absolute project folder (added by main, not persisted into file body)
  source_model: string | null;
  source_model_role: MeshRole | null;
  selected_object: string | null;
  working_model: string | null;
  working_model_fbx: string | null;
  approved_lowpoly_run_id: string | null;
  runs: string[];
}

export interface MeshObjectSummary {
  name: string;
  vertices: number;
  edges: number;
  faces: number;
  materials: string[];
  uv_layers: string[];
  bounds: { min: [number, number, number]; max: [number, number, number] } | null;
  mesh_role_hint: MeshRole;
}

export interface InspectResult {
  schema_version: number;
  status: 'accepted' | 'failed';
  command: string;
  project_id?: string | null;
  path?: string;
  objects?: MeshObjectSummary[];
  recommended_next_step?: NextStep;
  warnings?: string[];
  error?: WorkerError;
}

export interface GenerateOptions {
  mode?: 'decimation_optimize' | 'quad_retopo';
  preserve_features?: boolean;
  feature_angle?: number;
  apply_shrinkwrap?: boolean;
  retry_ladder?: boolean;
  // Number of retry-ladder escalation rungs to run when the primary collapse
  // misses the target band. 1 = single attempt (fast, default); 0 = disabled.
  retry_ladder_max_attempts?: number;
  render_preview?: boolean;
  // Large-mesh handling (plan §10): voxel-remesh to a proxy before decimation
  // when the source exceeds `proxy_face_threshold` faces.
  voxel_proxy?: boolean;
  proxy_face_threshold?: number;
  proxy_target_faces?: number;
}

export interface GenerateInput {
  command: typeof Command.GenerateLowpoly;
  project_id: string;
  run_id?: string;
  source_model?: string;
  object_name: string;
  target_faces: number;
  options?: GenerateOptions;
  out_dir?: string;
}

export interface WorkerError {
  code: string;
  message: string;
  traceback?: string;
}

export interface RunStatusDoc {
  schema_version: number;
  run_id: string;
  command: string;
  status: RunStatus;
  started_at: string;
  finished_at: string | null;
  input: Record<string, unknown>;
  artifacts: Record<string, string>;
  error: WorkerError | null;
}

export interface SummaryMetrics {
  source_faces: number | null;
  target_faces: number | null;
  actual_faces: number | null;
  target_error_ratio: number | null;
  non_manifold_edges: number | null;
  quad_ratio: number | null;
  triangle_ratio: number | null;
  ngon_count: number | null;
  surface_distance_mean_ratio: number | null;
  surface_distance_max_ratio: number | null;
  normal_deviation_mean_deg: number | null;
  volume_error_ratio: number | null;
}

export interface Summary {
  schema_version: number;
  run_id: string;
  command: string;
  object_name: string | null;
  result_object_name: string | null;
  method: string | null;
  metrics: SummaryMetrics;
  reports: {
    generation: string | null;
    validation: string | null;
    shape: string | null;
  };
  artifacts: Record<string, string>;
  warnings: string[];
}

/** Combined run view the renderer reads via `run:get` (plan §3 polling). */
export interface RunView {
  run_id: string;
  dir: string;
  status: RunStatusDoc | null;
  summary: Summary | null;
  reports: Record<string, unknown>; // raw per-phase reports for the report tabs
  stdout: string;
  stderr: string;
  preview_path: string | null; // absolute path to preview.png if present
}

export interface AppSettings {
  blenderPath: string | null;
  projectsRoot: string | null;
}

/** The API surface exposed on `window.api` by the preload bridge. */
export interface RendererApi {
  projectCreate(input: { name: string; sourcePath: string; role?: MeshRole }): Promise<Project>;
  projectOpen(dir: string): Promise<Project>;
  projectGet(projectId: string): Promise<Project>;
  modelInspect(input: { projectId: string; path?: string }): Promise<InspectResult>;
  lowpolyGenerate(input: {
    projectId: string;
    objectName: string;
    targetFaces: number;
    options?: GenerateOptions;
  }): Promise<{ run_id: string }>;
  lowpolyApprove(input: { projectId: string; runId: string }): Promise<{
    status: string;
    working_model: string;
    working_model_fbx: string | null;
    approved_lowpoly_run_id: string;
  }>;
  runGet(input: { projectId: string; runId: string }): Promise<RunView>;
  runList(projectId: string): Promise<string[]>;
  settingsGet(): Promise<AppSettings>;
  settingsSet(patch: Partial<AppSettings>): Promise<AppSettings>;
  pickFile(): Promise<string | null>;
  pickProjectDir(): Promise<string | null>;
  onRunUpdate(cb: (payload: { projectId: string; runId: string }) => void): () => void;
}
