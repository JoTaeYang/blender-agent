/**
 * Blender worker process orchestration (plan §3 Python/Blender Worker, §5).
 *
 * Spawns headless Blender for `inspect_model` and `generate_lowpoly`, captures
 * stdout/stderr to the run folder, and drives the `status.json` lifecycle. A mock
 * runner fabricates artifacts so the main process (and e2e smoke) work without a
 * Blender install (plan §8 "mock runner로 먼저 개발 가능").
 *
 * No `electron` import — constructed with explicit config so it is unit-testable.
 */

import { spawn } from 'child_process';
import { createWriteStream, existsSync, mkdirSync, writeFileSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import { randomUUID } from 'crypto';
import {
  Command,
  RunStatus,
  type GenerateOptions,
  type InspectResult,
} from '@shared/contracts';
import {
  ensureRunDir,
  getRunView,
  registerRun,
  newRunId,
  readProject,
  writeProject,
} from './project-service';
import * as fs from 'fs';

export interface WorkerConfig {
  blenderPath: string | null;
  workerRoot: string; // absolute path to repo `worker/`
  mock?: boolean; // force the mock runner (tests / no Blender)
  onRunUpdate?: (projectId: string, runId: string) => void;
}

export class WorkerSetupError extends Error {
  code = 'blender_not_configured';
}

export class WorkerRunner {
  constructor(private cfg: WorkerConfig) {}

  private useMock(): boolean {
    return this.cfg.mock === true || this.cfg.blenderPath === null;
  }

  private blenderArgs(script: string, after: string[]): string[] {
    return ['--background', '--python', join(this.cfg.workerRoot, script), '--', ...after];
  }

  // --- inspect_model ------------------------------------------------------
  async inspect(projectId: string, projectDir: string, sourcePath: string): Promise<InspectResult> {
    if (this.useMock()) {
      return mockInspect(projectId, sourcePath);
    }
    const outPath = join(tmpdir(), `inspect_${randomUUID()}.json`);
    const args = this.blenderArgs('inspect_model.py', [
      '--path', sourcePath,
      '--out', outPath,
      '--project-id', projectId,
    ]);
    await this.run(this.cfg.blenderPath as string, args);
    if (!existsSync(outPath)) {
      return {
        schema_version: 1,
        status: 'failed',
        command: Command.InspectModel,
        project_id: projectId,
        path: sourcePath,
        error: { code: 'no_output', message: 'inspect produced no result file' },
      };
    }
    return JSON.parse(fs.readFileSync(outPath, 'utf-8')) as InspectResult;
  }

  // --- generate_lowpoly ---------------------------------------------------
  /** Kick off generation asynchronously; returns immediately with the run id. */
  generate(
    projectId: string,
    projectDir: string,
    input: { sourceModel: string; objectName: string; targetFaces: number; options?: GenerateOptions },
  ): { run_id: string } {
    const runId = newRunId();
    const dir = ensureRunDir(projectDir, runId);
    registerRun(projectDir, runId);

    const appJob = {
      command: Command.GenerateLowpoly,
      project_id: projectId,
      run_id: runId,
      source_model: input.sourceModel,
      object_name: input.objectName,
      target_faces: input.targetFaces,
      options: {
        mode: 'decimation_optimize',
        preserve_features: true,
        feature_angle: 30.0,
        apply_shrinkwrap: true,
        retry_ladder: true,
        retry_ladder_max_attempts: 1,
        render_preview: true,
        // Large sources are voxel-remeshed to a proxy before decimation (plan §10).
        voxel_proxy: true,
        proxy_target_faces: 1_000_000,
        ...(input.options ?? {}),
      },
      out_dir: dir,
    };
    const jobPath = join(dir, 'app_job.json');
    writeFileSync(jobPath, JSON.stringify(appJob, null, 2));

    // Initial queued status so the renderer has something to poll immediately.
    writeFileSync(
      join(dir, 'status.json'),
      JSON.stringify(
        {
          schema_version: 1,
          run_id: runId,
          command: Command.GenerateLowpoly,
          status: RunStatus.Queued,
          started_at: new Date().toISOString(),
          finished_at: null,
          input: {
            source_model: input.sourceModel,
            object_name: input.objectName,
            target_faces: input.targetFaces,
          },
          artifacts: {},
          error: null,
        },
        null,
        2,
      ),
    );

    if (this.useMock()) {
      // Run the mock generation on the next tick so the caller gets the id first.
      setTimeout(() => {
        try {
          mockGenerate(dir, runId, appJob);
        } catch (err) {
          writeFailedStatus(dir, runId, input, String(err));
        }
        this.cfg.onRunUpdate?.(projectId, runId);
      }, 10);
      return { run_id: runId };
    }

    const args = this.blenderArgs('run_app_retopo_job.py', ['--job', jobPath]);
    this.run(this.cfg.blenderPath as string, args, dir)
      .catch((err) => writeFailedStatus(dir, runId, input, String(err)))
      .finally(() => this.cfg.onRunUpdate?.(projectId, runId));
    return { run_id: runId };
  }

  /** Spawn a process, tee stdout/stderr to the run folder when given. */
  private run(cmd: string, args: string[], logDir?: string): Promise<number> {
    return new Promise((resolve, reject) => {
      let child;
      try {
        child = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'] });
      } catch (err) {
        reject(err);
        return;
      }
      const out = logDir ? createWriteStream(join(logDir, 'stdout.log')) : null;
      const errStream = logDir ? createWriteStream(join(logDir, 'stderr.log')) : null;
      child.stdout?.on('data', (d) => out?.write(d));
      child.stderr?.on('data', (d) => errStream?.write(d));
      child.on('error', (err) => {
        out?.end();
        errStream?.end();
        reject(err);
      });
      child.on('close', (code) => {
        out?.end();
        errStream?.end();
        resolve(code ?? -1);
      });
    });
  }
}

function writeFailedStatus(
  dir: string,
  runId: string,
  input: { objectName: string; targetFaces: number },
  message: string,
): void {
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command: Command.GenerateLowpoly,
        status: RunStatus.Failed,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        input: { object_name: input.objectName, target_faces: input.targetFaces },
        artifacts: {},
        error: { code: 'spawn_failed', message },
      },
      null,
      2,
    ),
  );
}

// ---------------------------------------------------------------------------
// Mock runner — fabricates a deterministic accepted run without Blender.
// ---------------------------------------------------------------------------
function mockInspect(projectId: string, sourcePath: string): InspectResult {
  const faces = 121520;
  return {
    schema_version: 1,
    status: 'accepted',
    command: Command.InspectModel,
    project_id: projectId,
    path: sourcePath,
    objects: [
      {
        name: 'SM_Test_Pottery_a_02',
        vertices: 60842,
        edges: 182280,
        faces,
        materials: [],
        uv_layers: ['UVChannel_1'],
        bounds: { min: [-1, -1, -1], max: [1, 1, 1] },
        mesh_role_hint: 'highpoly',
      },
    ],
    recommended_next_step: 'generate_lowpoly',
    warnings: ['mock inspect: Blender path not configured'],
  };
}

function mockGenerate(dir: string, runId: string, appJob: any): void {
  mkdirSync(dir, { recursive: true });
  const target = Number(appJob.target_faces ?? 12000);
  const actual = Math.round(target * 1.012);
  const objectName = appJob.object_name ?? 'SM_Test_Pottery_a_02';

  const gen = {
    object_name: objectName,
    result_object_name: `${objectName}_low`,
    method: 'decimate_collapse',
    source_face_count: 121520,
    target_face_count: target,
    actual_face_count: actual,
    target_error_ratio: 0.012,
    band: 'accepted',
    notes: ['mock generation'],
  };
  const validation = {
    status: 'accepted',
    face_count: actual,
    target_face_count: target,
    quad_ratio: 0.0,
    triangle_ratio: 1.0,
    ngon_count: 0,
    non_manifold_edge_count: 0,
    reasons: [],
  };
  const shape = {
    status: 'accepted',
    surface_distance_mean_ratio: 0.0021,
    surface_distance_max_ratio: 0.0098,
    normal_deviation_mean_deg: 4.3,
    volume_error_ratio: 0.003,
    reasons: [],
  };
  writeFileSync(join(dir, 'generation_report.json'), JSON.stringify(gen, null, 2));
  writeFileSync(join(dir, 'validation_report.json'), JSON.stringify(validation, null, 2));
  writeFileSync(join(dir, 'shape_report.json'), JSON.stringify(shape, null, 2));
  // Minimal placeholder artifacts so approve/preview paths exist.
  writeFileSync(join(dir, 'lowpoly.blend'), 'MOCK-BLEND');
  writeFileSync(join(dir, 'lowpoly.fbx'), 'MOCK-FBX');
  writeFileSync(join(dir, 'preview.png'), MOCK_PNG);

  const summary = {
    schema_version: 1,
    run_id: runId,
    command: Command.GenerateLowpoly,
    object_name: objectName,
    result_object_name: `${objectName}_low`,
    method: 'decimate_collapse',
    metrics: {
      source_faces: 121520,
      target_faces: target,
      actual_faces: actual,
      target_error_ratio: 0.012,
      non_manifold_edges: 0,
      quad_ratio: 0.0,
      triangle_ratio: 1.0,
      ngon_count: 0,
      surface_distance_mean_ratio: 0.0021,
      surface_distance_max_ratio: 0.0098,
      normal_deviation_mean_deg: 4.3,
      volume_error_ratio: 0.003,
    },
    reports: { generation: 'accepted', validation: 'accepted', shape: 'accepted' },
    artifacts: {
      generation_report: 'generation_report.json',
      validation_report: 'validation_report.json',
      shape_report: 'shape_report.json',
      lowpoly_blend: 'lowpoly.blend',
      lowpoly_fbx: 'lowpoly.fbx',
      preview: 'preview.png',
    },
    warnings: ['mock generation: not a real Blender run'],
  };
  writeFileSync(join(dir, 'summary.json'), JSON.stringify(summary, null, 2));
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command: Command.GenerateLowpoly,
        status: RunStatus.Accepted,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        input: {
          source_model: appJob.source_model,
          object_name: objectName,
          target_faces: target,
        },
        artifacts: summary.artifacts,
        error: null,
      },
      null,
      2,
    ),
  );
}

// 1x1 transparent PNG (base64) used as the mock preview placeholder.
const MOCK_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
);

export { getRunView, readProject, writeProject };
