/**
 * Blender UV-review worker orchestration (plan §3 Worker, §5 Worker API, Session D).
 *
 * Spawns headless Blender for `inspect_uv_layers` and `review_existing_uv`,
 * captures stdout/stderr to the run folder, and drives the `status.json`
 * lifecycle. A mock runner fabricates artifacts so the main process (and the
 * renderer / e2e smoke) work without a Blender install (plan §11 "mock worker로
 * 선개발 가능"). MVP 1 is read-only — nothing here writes UVs or saves the model.
 *
 * No `electron` import — constructed with explicit config so it is unit-testable.
 */

import { spawn } from 'child_process';
import { createWriteStream, existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import { randomUUID } from 'crypto';
import {
  UvCommand,
  UvRunStatus,
  type InspectUvResult,
  type ReviewOptions,
} from '@shared/contracts';
import {
  ensureRunDir,
  newReviewRunId,
  registerReviewRun,
} from './project-service';

export interface UvWorkerConfig {
  blenderPath: string | null;
  workerRoot: string; // absolute path to repo `worker/`
  mock?: boolean; // force the mock runner (tests / no Blender)
  onRunUpdate?: (projectId: string, runId: string) => void;
}

export interface ReviewInput {
  modelAbs: string;
  modelRel: string;
  objectName: string;
  uvLayer: string;
  options?: ReviewOptions;
}

const DEFAULT_REVIEW_OPTIONS: ReviewOptions = {
  texture_size_px: 1024,
  checker_scale: 40.0,
  render_size_px: 900,
  raster_overlap_resolution: 1024,
  make_heatmaps: false,
  make_3q: false,
};

export class UvReviewRunner {
  constructor(private cfg: UvWorkerConfig) {}

  private useMock(): boolean {
    return this.cfg.mock === true || this.cfg.blenderPath === null;
  }

  private blenderArgs(after: string[]): string[] {
    return ['--background', '--python', join(this.cfg.workerRoot, 'review_existing_uv.py'), '--', ...after];
  }

  // --- inspect_uv_layers --------------------------------------------------
  async inspectLayers(projectId: string, modelAbs: string, modelRel: string): Promise<InspectUvResult> {
    if (this.useMock()) {
      return mockInspectUv(projectId, modelRel);
    }
    const outPath = join(tmpdir(), `uv_inspect_${randomUUID()}.json`);
    const jobPath = join(tmpdir(), `uv_inspect_job_${randomUUID()}.json`);
    writeFileSync(
      jobPath,
      JSON.stringify({
        command: UvCommand.InspectUvLayers,
        project_id: projectId,
        model: modelAbs,
        model_rel: modelRel,
        out: outPath,
      }),
    );
    await this.run(this.cfg.blenderPath as string, this.blenderArgs(['--job', jobPath]));
    if (!existsSync(outPath)) {
      return {
        schema_version: 1,
        status: 'failed',
        command: UvCommand.InspectUvLayers,
        project_id: projectId,
        model: modelRel,
        error: { code: 'no_output', message: 'inspect_uv_layers produced no result file' },
      };
    }
    return JSON.parse(readFileSync(outPath, 'utf-8')) as InspectUvResult;
  }

  // --- review_existing_uv -------------------------------------------------
  /** Kick off a review asynchronously; returns immediately with the run id. */
  reviewExisting(projectId: string, projectDir: string, input: ReviewInput): { run_id: string } {
    const runId = newReviewRunId();
    const dir = ensureRunDir(projectDir, runId);
    registerReviewRun(projectDir, runId);

    const job = {
      command: UvCommand.ReviewExistingUv,
      project_id: projectId,
      run_id: runId,
      model: input.modelAbs,
      model_rel: input.modelRel,
      object_name: input.objectName,
      uv_layer: input.uvLayer,
      options: { ...DEFAULT_REVIEW_OPTIONS, ...(input.options ?? {}) },
      out_dir: dir,
    };
    writeFileSync(join(dir, 'job.json'), JSON.stringify(job, null, 2));

    // Initial queued status so the renderer can poll immediately (plan §9).
    writeQueuedStatus(dir, runId, input);

    if (this.useMock()) {
      setTimeout(() => {
        try {
          mockReview(dir, runId, job);
        } catch (err) {
          writeFailedStatus(dir, runId, input, String(err));
        }
        this.cfg.onRunUpdate?.(projectId, runId);
      }, 10);
      return { run_id: runId };
    }

    this.run(this.cfg.blenderPath as string, this.blenderArgs(['--job', join(dir, 'job.json')]), dir)
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

function writeQueuedStatus(dir: string, runId: string, input: ReviewInput): void {
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command: UvCommand.ReviewExistingUv,
        status: UvRunStatus.Queued,
        started_at: new Date().toISOString(),
        finished_at: null,
        input: { model: input.modelRel, object_name: input.objectName, uv_layer: input.uvLayer },
        artifacts: {},
        error: null,
      },
      null,
      2,
    ),
  );
}

function writeFailedStatus(dir: string, runId: string, input: ReviewInput, message: string): void {
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command: UvCommand.ReviewExistingUv,
        status: UvRunStatus.Failed,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        input: { model: input.modelRel, object_name: input.objectName, uv_layer: input.uvLayer },
        artifacts: {},
        error: { code: 'spawn_failed', message },
      },
      null,
      2,
    ),
  );
}

// ---------------------------------------------------------------------------
// Mock runner — fabricates a deterministic accepted review without Blender.
// ---------------------------------------------------------------------------
function mockInspectUv(projectId: string, modelRel: string): InspectUvResult {
  return {
    schema_version: 1,
    status: 'accepted',
    command: UvCommand.InspectUvLayers,
    project_id: projectId,
    model: modelRel,
    objects: [
      {
        name: 'SM_Test_Pottery_a_02',
        vertices: 6562,
        edges: 18701,
        faces: 12152,
        materials: [],
        uv_layers: [{ name: 'UVChannel_1', active: true, loop_count: 36896, empty: false }],
        active_uv_layer: 'UVChannel_1',
        has_uv: true,
      },
    ],
    recommended_next_step: 'review_existing_uv',
    warnings: ['mock inspect: Blender path not configured'],
  };
}

function mockReview(dir: string, runId: string, job: any): void {
  mkdirSync(dir, { recursive: true });
  const objectName = job.object_name ?? 'SM_Test_Pottery_a_02';
  const uvLayer = job.uv_layer ?? 'UVChannel_1';

  const metrics = {
    stretch_score: 0.06866,
    worst_island_distortion: 0.202999,
    overlap_ratio: 0.0,
    raster_overlap_ratio: 0.0,
    self_overlap_ratio: 0.0,
    cross_overlap_ratio: 0.0,
    texel_density_variance: 0.000002,
    packing_efficiency: 0.591278,
  };
  const uv = {
    island_count: 43,
    uv_bounds: { min: [0.001, 0.002], max: [0.998, 0.997], in_0_1: true },
    has_negative_uv: false,
    has_out_of_tile_uv: false,
  };
  const artifacts = {
    metrics: 'uv_metrics.json',
    uv_layers: 'uv_layers.json',
    uv_bounds: 'uv_bounds.json',
    uv_layout: 'uv_layout.png',
    uv_layout_svg: 'uv_layout.svg',
    checker_front: 'checker_front.png',
    checker_side: 'checker_side.png',
  };

  writeFileSync(join(dir, 'uv_metrics.json'), JSON.stringify({ metrics, uv, islands: [] }, null, 2));
  writeFileSync(join(dir, 'uv_layers.json'), JSON.stringify([{ name: uvLayer, active: true, loop_count: 36896, empty: false }], null, 2));
  writeFileSync(join(dir, 'uv_bounds.json'), JSON.stringify(uv.uv_bounds, null, 2));
  writeFileSync(join(dir, 'uv_layout.svg'), '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"></svg>');
  for (const png of ['uv_layout.png', 'checker_front.png', 'checker_side.png']) {
    writeFileSync(join(dir, png), MOCK_PNG);
  }

  const summary = {
    schema_version: 1,
    run_id: runId,
    command: UvCommand.ReviewExistingUv,
    status: UvRunStatus.Accepted,
    model: job.model_rel ?? null,
    object_name: objectName,
    uv_layer: uvLayer,
    mesh: { vertices: 6562, edges: 18701, faces: 12152, loops: 36896 },
    uv,
    metrics,
    review_status: 'clean',
    issues: [],
    artifacts,
    warnings: ['mock review: not a real Blender run'],
  };
  writeFileSync(join(dir, 'uv_review_summary.json'), JSON.stringify(summary, null, 2));
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        run_id: runId,
        command: UvCommand.ReviewExistingUv,
        status: UvRunStatus.Accepted,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        input: { model: job.model_rel, object_name: objectName, uv_layer: uvLayer },
        artifacts,
        error: null,
      },
      null,
      2,
    ),
  );
}

// 1x1 transparent PNG (base64) used as the mock image placeholder.
const MOCK_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
);
