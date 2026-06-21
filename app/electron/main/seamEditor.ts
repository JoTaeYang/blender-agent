/**
 * Blender seam-editor worker orchestration (plan §5, §6, §11 Session D).
 *
 * Two kinds of operation:
 *
 * - **Blender-backed** (`exportEdgeGeometry`, `extractUvBoundary`) spawn headless
 *   Blender, tee logs to the run folder, and drive the `status.json` lifecycle. A
 *   mock fabricates a deterministic cube / boundary so the app + renderer work
 *   without a Blender install (plan §11 "mock worker로 선개발 가능").
 * - **Pure-Node** (`validateSpec`, `saveSpec`, `loadSpec`) need no Blender: the
 *   renderer already holds the mesh edge count from the edge-geometry export, so
 *   the canonical spec is validated/normalized/written in-process for instant UX
 *   (plan §6.2/§6.3 rules, shared with the Python worker via
 *   `normalizeAndValidateSpec`).
 *
 * MVP 2 is non-generative — nothing here unwraps, packs, or marks seams on the
 * mesh. No `electron` import; constructed with explicit config so it is unit-testable.
 */

import { spawn } from 'child_process';
import { createWriteStream, existsSync, readFileSync, writeFileSync } from 'fs';
import { isAbsolute, join } from 'path';
import {
  SeamCommand,
  SeamRunStatus,
  makeSeamSpec,
  normalizeAndValidateSpec,
  type EdgeGeometry,
  type SeamSpec,
  type SeamValidation,
} from '@shared/contracts';
import {
  ensureRunDir,
  newSeamRunId,
  readProject,
  registerSeamEditorRun,
  seamsDir,
  setActiveUserSeamSpec,
} from './project-service';

export interface SeamWorkerConfig {
  blenderPath: string | null;
  workerRoot: string; // absolute path to repo `worker/`
  mock?: boolean; // force the mock runner (tests / no Blender)
  onRunUpdate?: (projectId: string, runId: string) => void;
}

export interface ExportInput {
  modelAbs: string;
  modelRel: string;
  objectName: string;
}

export interface ExtractInput {
  modelAbs: string;
  modelRel: string;
  objectName: string;
  uvLayer?: string;
}

/** Canonical spec filenames under `work/seams/` (plan §2 folder layout). */
const USER_SPEC_REL = join('work', 'seams', 'user_seam_spec.json');
const BOUNDARY_SPEC_REL = join('work', 'seams', 'reference_boundary_seam_spec.json');

export class SeamEditorRunner {
  constructor(private cfg: SeamWorkerConfig) {}

  private useMock(): boolean {
    return this.cfg.mock === true || this.cfg.blenderPath === null;
  }

  private blenderArgs(after: string[]): string[] {
    return [
      '--background',
      '--python',
      join(this.cfg.workerRoot, 'seam_editor_worker.py'),
      '--',
      ...after,
    ];
  }

  // --- export_edge_geometry (plan §5.1) ----------------------------------
  /** Kick off an edge-geometry export asynchronously; returns the run id. */
  exportEdgeGeometry(projectId: string, projectDir: string, input: ExportInput): { run_id: string } {
    const runId = newSeamRunId();
    const dir = ensureRunDir(projectDir, runId);
    registerSeamEditorRun(projectDir, runId);

    const job = {
      command: SeamCommand.ExportEdgeGeometry,
      project_id: projectId,
      run_id: runId,
      model: input.modelAbs,
      model_rel: input.modelRel,
      object_name: input.objectName,
      out_dir: dir,
    };
    writeFileSync(join(dir, 'job.json'), JSON.stringify(job, null, 2));
    writeQueuedStatus(dir, runId, SeamCommand.ExportEdgeGeometry, input);

    if (this.useMock()) {
      setTimeout(() => {
        try {
          mockExportEdgeGeometry(dir, runId, input.objectName, input.modelRel);
        } catch (err) {
          writeFailedStatus(dir, runId, SeamCommand.ExportEdgeGeometry, input, String(err));
        }
        this.cfg.onRunUpdate?.(projectId, runId);
      }, 10);
      return { run_id: runId };
    }

    this.run(this.cfg.blenderPath as string, this.blenderArgs(['--job', join(dir, 'job.json')]), dir)
      .catch((err) => writeFailedStatus(dir, runId, SeamCommand.ExportEdgeGeometry, input, String(err)))
      .finally(() => this.cfg.onRunUpdate?.(projectId, runId));
    return { run_id: runId };
  }

  // --- extract_uv_boundary_as_seams (plan §6.4) --------------------------
  /** Kick off a UV-boundary extraction asynchronously; returns the run id. */
  extractUvBoundary(projectId: string, projectDir: string, input: ExtractInput): { run_id: string } {
    const runId = newSeamRunId();
    const dir = ensureRunDir(projectDir, runId);
    registerSeamEditorRun(projectDir, runId);
    seamsDir(projectDir); // ensure work/seams exists for the boundary spec

    const job = {
      command: SeamCommand.ExtractUvBoundary,
      project_id: projectId,
      run_id: runId,
      model: input.modelAbs,
      model_rel: input.modelRel,
      object_name: input.objectName,
      uv_layer: input.uvLayer ?? null,
      out_dir: dir,
      out_path: join(projectDir, BOUNDARY_SPEC_REL),
      out_path_rel: BOUNDARY_SPEC_REL,
    };
    writeFileSync(join(dir, 'job.json'), JSON.stringify(job, null, 2));
    writeQueuedStatus(dir, runId, SeamCommand.ExtractUvBoundary, input);

    if (this.useMock()) {
      setTimeout(() => {
        try {
          mockExtractUvBoundary(dir, runId, projectDir, input);
        } catch (err) {
          writeFailedStatus(dir, runId, SeamCommand.ExtractUvBoundary, input, String(err));
        }
        this.cfg.onRunUpdate?.(projectId, runId);
      }, 10);
      return { run_id: runId };
    }

    this.run(this.cfg.blenderPath as string, this.blenderArgs(['--job', join(dir, 'job.json')]), dir)
      .catch((err) => writeFailedStatus(dir, runId, SeamCommand.ExtractUvBoundary, input, String(err)))
      .finally(() => this.cfg.onRunUpdate?.(projectId, runId));
    return { run_id: runId };
  }

  // --- validate / save / load (pure Node, plan §6.1–§6.3) ----------------
  /** Validate + normalize a spec against the known mesh edge count (no Blender). */
  validateSpec(input: {
    spec: SeamSpec;
    objectName: string;
    edgeCount?: number | null;
  }): SeamValidation {
    return normalizeAndValidateSpec(input.spec, {
      edgeCount: input.edgeCount ?? null,
      objectName: input.objectName,
    });
  }

  /**
   * Normalize + write the canonical `user_seam_spec.json` and record it as the
   * active spec for the MVP 3 handoff (plan §6.2, §9, §10). Returns the
   * project-relative path so the UI never sees an absolute path (plan §9).
   */
  saveSpec(
    projectDir: string,
    input: { spec: SeamSpec; objectName: string; edgeCount?: number | null },
  ): { status: string; path: string; validation: SeamValidation } {
    const validation = normalizeAndValidateSpec(input.spec, {
      edgeCount: input.edgeCount ?? null,
      objectName: input.objectName,
    });
    seamsDir(projectDir);
    writeFileSync(
      join(projectDir, USER_SPEC_REL),
      JSON.stringify(validation.normalized_spec, null, 2),
    );
    setActiveUserSeamSpec(projectDir, USER_SPEC_REL, input.objectName);
    return { status: 'accepted', path: USER_SPEC_REL, validation };
  }

  /**
   * Read a spec file + validate it (plan §6.1). `path` may be absolute or
   * project-relative; defaults to the project's active spec / `user_seam_spec.json`.
   * Returns nulls (not an error) when no spec file exists yet.
   */
  loadSpec(
    projectDir: string,
    input: { path?: string; objectName: string; edgeCount?: number | null },
  ): { spec: SeamSpec | null; validation: SeamValidation | null; path: string | null } {
    const project = readProject(projectDir);
    const rel = input.path ?? project.active_user_seam_spec ?? USER_SPEC_REL;
    const abs = isAbsolute(rel) ? rel : join(projectDir, rel);
    if (!existsSync(abs)) {
      return { spec: null, validation: null, path: null };
    }
    const spec = JSON.parse(readFileSync(abs, 'utf-8')) as SeamSpec;
    const validation = normalizeAndValidateSpec(spec, {
      edgeCount: input.edgeCount ?? null,
      objectName: input.objectName,
    });
    return { spec, validation, path: rel };
  }

  /** Spawn a process, tee stdout/stderr to the run folder. */
  private run(cmd: string, args: string[], logDir: string): Promise<number> {
    return new Promise((resolve, reject) => {
      let child;
      try {
        child = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'] });
      } catch (err) {
        reject(err);
        return;
      }
      const out = createWriteStream(join(logDir, 'stdout.log'));
      const errStream = createWriteStream(join(logDir, 'stderr.log'));
      child.stdout?.on('data', (d) => out.write(d));
      child.stderr?.on('data', (d) => errStream.write(d));
      child.on('error', (err) => {
        out.end();
        errStream.end();
        reject(err);
      });
      child.on('close', (code) => {
        out.end();
        errStream.end();
        resolve(code ?? -1);
      });
    });
  }
}

// ---------------------------------------------------------------------------
// status.json helpers (plan §9)
// ---------------------------------------------------------------------------
function statusInput(input: ExportInput | ExtractInput): Record<string, unknown> {
  return {
    model: input.modelRel,
    object_name: input.objectName,
    uv_layer: 'uvLayer' in input ? (input.uvLayer ?? null) : null,
  };
}

function writeQueuedStatus(
  dir: string,
  runId: string,
  command: string,
  input: ExportInput | ExtractInput,
): void {
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command,
        status: SeamRunStatus.Queued,
        started_at: new Date().toISOString(),
        finished_at: null,
        input: statusInput(input),
        artifacts: {},
        error: null,
      },
      null,
      2,
    ),
  );
}

function writeFailedStatus(
  dir: string,
  runId: string,
  command: string,
  input: ExportInput | ExtractInput,
  message: string,
): void {
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command,
        status: SeamRunStatus.Failed,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        input: statusInput(input),
        artifacts: {},
        error: { code: 'spawn_failed', message },
      },
      null,
      2,
    ),
  );
}

// ---------------------------------------------------------------------------
// Mock runner — fabricates a deterministic cube + boundary without Blender.
// ---------------------------------------------------------------------------
/** Build edge geometry from quad faces, mirroring MeshGraph.from_faces ordering. */
function edgeGeometryFromFaces(
  objectName: string,
  verts: [number, number, number][],
  faces: number[][],
): EdgeGeometry {
  const edgeKey = (a: number, b: number) => (a < b ? `${a},${b}` : `${b},${a}`);
  const edgeIndex = new Map<string, number>();
  const edgeVerts: [number, number][] = [];
  const edgeFaces: number[][] = [];
  const faceEdgeIds: number[][] = [];

  faces.forEach((vids, fid) => {
    const ids: number[] = [];
    for (let i = 0; i < vids.length; i++) {
      const a = vids[i];
      const b = vids[(i + 1) % vids.length];
      const key = edgeKey(a, b);
      let idx = edgeIndex.get(key);
      if (idx === undefined) {
        idx = edgeVerts.length;
        edgeIndex.set(key, idx);
        edgeVerts.push(a < b ? [a, b] : [b, a]);
        edgeFaces.push([]);
      }
      edgeFaces[idx].push(fid);
      ids.push(idx);
    }
    faceEdgeIds[fid] = ids;
  });

  return {
    schema_version: 1,
    object: objectName,
    vertices: verts.map((co, id) => ({ id, co })),
    edges: edgeVerts.map((vertex_ids, id) => {
      const fids = edgeFaces[id];
      return {
        id,
        vertex_ids,
        face_ids: fids,
        is_boundary: fids.length === 1,
        is_non_manifold: fids.length > 2,
        is_sharp: false,
        is_seam: false,
        dihedral_angle: 90.0,
      };
    }),
    faces: faces.map((vertex_ids, id) => ({
      id,
      vertex_ids,
      edge_ids: faceEdgeIds[id],
      material_index: 0,
    })),
  };
}

// A unit cube (8 verts, 6 quad faces, 12 edges) — enough real selectable edges
// for the renderer to exercise selection / seam / protect without Blender.
const CUBE_VERTS: [number, number, number][] = [
  [-1, -1, -1],
  [1, -1, -1],
  [1, 1, -1],
  [-1, 1, -1],
  [-1, -1, 1],
  [1, -1, 1],
  [1, 1, 1],
  [-1, 1, 1],
];
const CUBE_FACES: number[][] = [
  [0, 3, 2, 1],
  [4, 5, 6, 7],
  [0, 1, 5, 4],
  [1, 2, 6, 5],
  [2, 3, 7, 6],
  [3, 0, 4, 7],
];

function mockExportEdgeGeometry(dir: string, runId: string, objectName: string, modelRel: string): void {
  const geometry = edgeGeometryFromFaces(objectName || 'MockCube', CUBE_VERTS, CUBE_FACES);
  writeFileSync(join(dir, 'edge_geometry.json'), JSON.stringify(geometry, null, 2));

  const signature = {
    vertices: geometry.vertices.length,
    edges: geometry.edges.length,
    faces: geometry.faces.length,
    loops: CUBE_FACES.reduce((n, f) => n + f.length, 0),
  };
  const artifacts = { edge_geometry: 'edge_geometry.json' };
  const result = {
    schema_version: 1,
    status: SeamRunStatus.Accepted,
    command: SeamCommand.ExportEdgeGeometry,
    object_name: geometry.object,
    mesh_signature: signature,
    artifacts,
    warnings: ['mock export: Blender path not configured'],
  };
  writeFileSync(join(dir, 'export_result.json'), JSON.stringify(result, null, 2));
  writeAcceptedStatus(dir, runId, SeamCommand.ExportEdgeGeometry, modelRel, objectName, artifacts);
}

function mockExtractUvBoundary(
  dir: string,
  runId: string,
  projectDir: string,
  input: ExtractInput,
): void {
  // A plausible boundary on the mock cube: the 4 vertical edges (ids depend on
  // traversal). Use a small fixed set the renderer can preview as a draft.
  const seamEdges = [1, 3, 5, 7];
  const spec = makeSeamSpec({
    object: input.objectName || 'MockCube',
    user_seam_edges: seamEdges,
    notes: `Extracted from UV island boundaries: ${input.uvLayer ?? 'UVChannel_1'}`,
  });
  seamsDir(projectDir);
  writeFileSync(join(projectDir, BOUNDARY_SPEC_REL), JSON.stringify(spec, null, 2));

  const artifacts = { boundary_report: 'boundary_extract_report.json', boundary_spec: BOUNDARY_SPEC_REL };
  const result = {
    schema_version: 1,
    status: SeamRunStatus.Accepted,
    command: SeamCommand.ExtractUvBoundary,
    path: BOUNDARY_SPEC_REL,
    object_name: spec.object,
    uv_layer: input.uvLayer ?? 'UVChannel_1',
    user_seam_count: seamEdges.length,
    user_protected_count: 0,
    spec,
    report: {
      boundary_edge_count: seamEdges.length,
      mesh_boundary_edges: [],
      ambiguous_edges: [],
      non_manifold_edges: [],
      uv_layer_missing: false,
    },
    warnings: ['mock extract: not a real Blender run'],
  };
  writeFileSync(join(dir, 'boundary_extract_report.json'), JSON.stringify(result, null, 2));
  writeAcceptedStatus(dir, runId, SeamCommand.ExtractUvBoundary, input.modelRel, input.objectName, artifacts);
}

function writeAcceptedStatus(
  dir: string,
  runId: string,
  command: string,
  modelRel: string,
  objectName: string,
  artifacts: Record<string, string>,
): void {
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command,
        status: SeamRunStatus.Accepted,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        input: { model: modelRel, object_name: objectName, uv_layer: null },
        artifacts,
        error: null,
      },
      null,
      2,
    ),
  );
}
