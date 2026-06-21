/**
 * Blender production-export worker orchestration (MVP 5 plan §5, §11, §12 Session E).
 *
 * Spawns headless Blender for `export_production_asset`, captures stdout/stderr to
 * the export folder, drives the `status.json` lifecycle, and — on a shipped run
 * (accepted/partial) — records `latest_export_id` + an `export_created` history
 * event (plan §6, §8). A mock runner fabricates a deterministic accepted export
 * (FBX/OBJ/GLB files + manifest + validation + previews) so the app + renderer +
 * e2e smoke work without a Blender install (plan §12 "mock export records").
 *
 * `checkReadiness` is a cheap pure-Node pre-flight that mirrors the Python
 * contract: it reads the MVP 3 `selected_uv_summary.json` and blocks export
 * unless the selected UV run is accepted, in-bounds, overlap-free and
 * seam-integrity-valid (plan §0, §4). MVP 4 AI Review is skipped and never
 * blocks (plan §0). The worker never mutates the source selected UV blend (plan §11).
 *
 * No `electron` import — constructed with explicit config so it is unit-testable.
 */

import { ChildProcess, spawn } from 'child_process';
import {
  createWriteStream,
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from 'fs';
import { isAbsolute, join } from 'path';
import {
  ExportCommand,
  ExportRunStatus,
  SUPPORTED_EXPORT_FORMATS,
  mergeExportOptions,
  type ExportBlockingIssue,
  type ExportOptions,
  type ExportReadiness,
  type ExportReadinessChecks,
} from '@shared/contracts';
import {
  SELECTED_UV_BLEND_REL,
  SELECTED_UV_SUMMARY_REL,
  ensureExportDir,
  newExportId,
  readProject,
  recordExportOutcome,
  registerExport,
} from './project-service';

export interface ExportWorkerConfig {
  blenderPath: string | null;
  workerRoot: string; // absolute path to repo `worker/`
  mock?: boolean; // force the mock runner (tests / no Blender)
  onRunUpdate?: (projectId: string, exportId: string) => void;
}

const RASTER_OVERLAP_MAX = 0.005;

// Readiness check -> blocking issue (code, message). Mirror of the Python
// contract `READINESS_BLOCKERS` (plan §4). Only these checks block export.
const READINESS_BLOCKERS: Record<string, ExportBlockingIssue> = {
  model_exists: { code: 'missing_selected_uv_model', message: 'Run MVP 3 Generate + Optimize before export.' },
  summary_exists: { code: 'missing_selected_uv_summary', message: 'Selected UV summary is missing; re-run MVP 3 Generate + Optimize.' },
  uv_run_accepted: { code: 'uv_run_not_accepted', message: 'Selected UV run is not accepted; resolve seam/overlap review in MVP 3.' },
  raster_overlap_ok: { code: 'raster_overlap', message: 'Selected UV has raster overlap; it cannot be exported.' },
  uv_bounds_ok: { code: 'uv_out_of_bounds', message: 'Selected UV is outside the [0,1] tile; it cannot be exported.' },
  seam_integrity_ok: { code: 'seam_integrity_failed', message: 'Selected UV failed seam integrity; revisit the MVP 2 Seam Editor.' },
};

function absFromRel(projectDir: string, rel: string | null | undefined): string | null {
  if (!rel) return null;
  return isAbsolute(rel) ? rel : join(projectDir, rel);
}

/** Lower-case, de-duplicate, keep only supported formats, in request order. */
function normalizeFormats(formats: string[] | undefined): string[] {
  const supported = SUPPORTED_EXPORT_FORMATS as readonly string[];
  const out: string[] = [];
  for (const f of formats ?? []) {
    const key = String(f).trim().toLowerCase().replace(/^\./, '');
    if (supported.includes(key) && !out.includes(key)) out.push(key);
  }
  return out;
}

function exportFilename(options: ExportOptions, objectName: string, fmt: string): string {
  const base = (options.export_name ?? '').trim() || `${objectName || 'model'}_low_uv`;
  return `${base}.${fmt}`;
}

export class ExportRunner {
  private running = new Map<string, ChildProcess>();

  constructor(private cfg: ExportWorkerConfig) {}

  /** Pick up a changed Blender path without dropping the live `running` map. */
  setBlenderPath(path: string | null): void {
    this.cfg.blenderPath = path;
  }

  private useMock(): boolean {
    return this.cfg.mock === true || this.cfg.blenderPath === null;
  }

  private blenderArgs(after: string[]): string[] {
    return ['--background', '--python', join(this.cfg.workerRoot, 'export_production_asset.py'), '--', ...after];
  }

  // --- checkReadiness (plan §4 — pure-Node pre-flight) -------------------
  /** Readiness from the MVP 3 selected UV summary; mirrors the Python contract. */
  checkReadiness(projectDir: string): ExportReadiness {
    const project = readProject(projectDir);
    const modelRel = project.selected_uv_model ?? null;
    const summaryRel = project.selected_uv_summary ?? null;
    const modelAbs = absFromRel(projectDir, modelRel);
    const summaryAbs = absFromRel(projectDir, summaryRel);

    const modelExists = !!(modelAbs && existsSync(modelAbs));
    let summary: Record<string, unknown> | null = null;
    if (summaryAbs && existsSync(summaryAbs)) {
      try {
        summary = JSON.parse(readFileSync(summaryAbs, 'utf-8'));
      } catch {
        summary = null;
      }
    }
    const summaryExists = summary !== null;
    const metrics = (summary?.metrics ?? {}) as Record<string, unknown>;
    const integrity = (summary?.seam_integrity ?? {}) as Record<string, unknown>;
    const raster = typeof metrics.raster_overlap_ratio === 'number' ? metrics.raster_overlap_ratio : 0;

    const checks: ExportReadinessChecks = {
      model_exists: modelExists,
      summary_exists: summaryExists,
      uv_run_accepted: summaryExists && summary?.status === 'accepted',
      raster_overlap_ok: summaryExists && raster <= RASTER_OVERLAP_MAX,
      uv_bounds_ok: summaryExists && metrics.uv_bounds_ok !== false,
      seam_integrity_ok: summaryExists && integrity.valid === true,
      ai_review_required: false,
      ai_review_skipped: true,
    };

    const blocking: ExportBlockingIssue[] = [];
    for (const [key, issue] of Object.entries(READINESS_BLOCKERS)) {
      if (!checks[key as keyof ExportReadinessChecks]) blocking.push(issue);
    }
    const ready = blocking.length === 0;
    return {
      schema_version: 1,
      status: ready ? ExportRunStatus.Accepted : 'needs_input',
      ready,
      selected_uv_model: modelRel,
      source_uv_run_id: project.latest_uv_generate_run_id ?? null,
      checks,
      blocking_issues: blocking,
      warnings: ['AI Review was skipped.'],
    };
  }

  // --- export_production_asset (plan §5.1) -------------------------------
  /** Kick off an export run asynchronously; returns the export id. */
  start(
    projectId: string,
    projectDir: string,
    input: { formats: string[]; options?: ExportOptions },
  ): { export_id: string } {
    const project = readProject(projectDir);
    const formats = normalizeFormats(input.formats);
    const options = mergeExportOptions(input.options);

    const exportId = newExportId();
    const dir = ensureExportDir(projectDir, exportId);
    registerExport(projectDir, exportId);

    const objectName = project.selected_object ?? '';
    const modelRel = project.selected_uv_model ?? SELECTED_UV_BLEND_REL;
    const summaryRel = project.selected_uv_summary ?? SELECTED_UV_SUMMARY_REL;
    const uvRunId = project.latest_uv_generate_run_id ?? null;
    const seamSpecRel = project.active_user_seam_spec ?? null;

    // Read the MVP 3 selected candidate id for the manifest/history (best-effort).
    let selectedCandidateId: string | null = null;
    const summaryAbs = absFromRel(projectDir, summaryRel);
    if (summaryAbs && existsSync(summaryAbs)) {
      try {
        selectedCandidateId = JSON.parse(readFileSync(summaryAbs, 'utf-8')).selected_candidate_id ?? null;
      } catch {
        /* best-effort */
      }
    }

    const job = {
      command: ExportCommand.ExportProductionAsset,
      project_id: projectId,
      export_id: exportId,
      selected_uv_model: absFromRel(projectDir, modelRel),
      selected_uv_model_rel: modelRel,
      selected_uv_summary: summaryAbs,
      selected_uv_summary_rel: summaryRel,
      object_name: objectName,
      formats,
      options,
      out_dir: dir,
      out_dir_rel: join('exports', exportId),
      uv_generate_run_id: uvRunId,
      seam_spec_rel: seamSpecRel,
      candidate_summary_rel: uvRunId ? join('runs', uvRunId, 'candidate_summary.json') : null,
      p5_gate_rel: uvRunId ? join('runs', uvRunId, 'p5_gate.json') : null,
      seam_report_rel: uvRunId ? join('runs', uvRunId, 'seam_report.json') : null,
      selected_candidate_id: selectedCandidateId,
      out: join(dir, 'export_result.json'),
    };
    writeFileSync(join(dir, 'job.json'), JSON.stringify(job, null, 2));
    writeQueuedStatus(dir, exportId, formats);

    const finish = () => {
      this.running.delete(exportId);
      try {
        recordExportOutcome(projectDir, exportId);
      } catch {
        /* manifest/history update is best-effort; the run view still reads status.json */
      }
      this.cfg.onRunUpdate?.(projectId, exportId);
    };

    if (formats.length === 0) {
      writeFailedStatus(dir, exportId, [], 'no_formats', 'no supported export formats requested');
      setTimeout(finish, 5);
      return { export_id: exportId };
    }

    if (this.useMock()) {
      setTimeout(() => {
        try {
          mockExport(dir, exportId, projectDir, job, objectName, selectedCandidateId);
        } catch (err) {
          writeFailedStatus(dir, exportId, formats, 'mock_failed', String(err));
        }
        finish();
      }, 10);
      return { export_id: exportId };
    }

    const child = this.run(
      this.cfg.blenderPath as string,
      this.blenderArgs(['--job', join(dir, 'job.json')]),
      dir,
      (err) => writeFailedStatus(dir, exportId, formats, 'spawn_failed', String(err)),
      finish,
    );
    if (child) this.running.set(exportId, child);
    return { export_id: exportId };
  }

  /** Cancel a running export job (plan §12 worker cancel). */
  cancel(projectDir: string, exportId: string): { status: string } {
    const child = this.running.get(exportId);
    if (child) {
      child.kill();
      this.running.delete(exportId);
    }
    const dir = ensureExportDir(projectDir, exportId);
    if (existsSync(join(dir, 'status.json'))) {
      try {
        const status = JSON.parse(readFileSync(join(dir, 'status.json'), 'utf-8'));
        if (!['accepted', 'partial', 'failed'].includes(status.status)) {
          status.status = ExportRunStatus.Cancelled;
          status.finished_at = new Date().toISOString();
          writeFileSync(join(dir, 'status.json'), JSON.stringify(status, null, 2));
        }
      } catch {
        /* leave the on-disk status as-is if it cannot be parsed */
      }
    }
    return { status: 'cancelled' };
  }

  /** Spawn a process, tee stdout/stderr to the export folder; returns the child. */
  private run(
    cmd: string,
    args: string[],
    logDir: string,
    onError: (err: unknown) => void,
    onDone: () => void,
  ): ChildProcess | null {
    let child: ChildProcess;
    try {
      child = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    } catch (err) {
      onError(err);
      onDone();
      return null;
    }
    const out = createWriteStream(join(logDir, 'stdout.log'));
    const errStream = createWriteStream(join(logDir, 'stderr.log'));
    child.stdout?.on('data', (d) => out.write(d));
    child.stderr?.on('data', (d) => errStream.write(d));
    child.on('error', (err) => {
      out.end();
      errStream.end();
      onError(err);
      onDone();
    });
    child.on('close', () => {
      out.end();
      errStream.end();
      onDone();
    });
    return child;
  }
}

// ---------------------------------------------------------------------------
// status.json helpers (plan §6)
// ---------------------------------------------------------------------------
function statusInput(formats: string[]): Record<string, unknown> {
  return { selected_uv_model: SELECTED_UV_BLEND_REL, formats };
}

function writeQueuedStatus(dir: string, exportId: string, formats: string[]): void {
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        export_id: exportId,
        command: ExportCommand.ExportProductionAsset,
        status: ExportRunStatus.Queued,
        started_at: new Date().toISOString(),
        finished_at: null,
        input: statusInput(formats),
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
  exportId: string,
  formats: string[],
  code: string,
  message: string,
): void {
  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        export_id: exportId,
        command: ExportCommand.ExportProductionAsset,
        status: ExportRunStatus.Failed,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        input: statusInput(formats),
        artifacts: {},
        error: { code, message },
      },
      null,
      2,
    ),
  );
}

// ---------------------------------------------------------------------------
// Mock runner — fabricates a deterministic accepted export without Blender.
// Shapes mirror the plan §5.1 / §6 / §7 examples so the renderer + e2e smoke work.
// ---------------------------------------------------------------------------
function mockExport(
  dir: string,
  exportId: string,
  projectDir: string,
  job: any,
  objectName: string,
  selectedCandidateId: string | null,
): void {
  mkdirSync(dir, { recursive: true });
  const options = mergeExportOptions(job.options);
  const formats: string[] = job.formats ?? [];

  // Pull metrics off the MVP 3 selected UV summary when present (else defaults).
  let metrics: Record<string, number> = {
    stretch_score: 0.06866,
    worst_island_distortion: 0.202999,
    raster_overlap_ratio: 0.0,
    texel_density_variance: 0.000002,
    packing_efficiency: 0.591278,
  };
  if (job.selected_uv_summary && existsSync(job.selected_uv_summary)) {
    try {
      const m = JSON.parse(readFileSync(job.selected_uv_summary, 'utf-8')).metrics ?? {};
      metrics = {
        stretch_score: m.stretch_score ?? metrics.stretch_score,
        worst_island_distortion: m.worst_island_distortion ?? metrics.worst_island_distortion,
        raster_overlap_ratio: m.raster_overlap_ratio ?? 0.0,
        texel_density_variance: m.texel_density_variance ?? metrics.texel_density_variance,
        packing_efficiency: m.packing_efficiency ?? metrics.packing_efficiency,
      };
    } catch {
      /* defaults */
    }
  }

  const obj = objectName || 'SM_Test_Pottery_a_02';
  const activeUv = options.selected_uv_layer || 'UVChannel_1';

  // Export files + previews (placeholder bytes — never opened by tests).
  const nameFiles: Record<string, string> = {};
  for (const fmt of formats) {
    const filename = exportFilename(options, obj, fmt);
    nameFiles[fmt] = filename;
    writeFileSync(join(dir, filename), `MOCK-${fmt.toUpperCase()}`);
  }
  for (const png of ['uv_layout.png', 'checker_front.png', 'checker_side.png']) {
    writeFileSync(join(dir, png), MOCK_PNG);
  }

  // Per-format validation: re-open ok, UV present (named UVMap for obj/glb).
  const validationFormats: Record<string, unknown> = {};
  for (const fmt of formats) {
    const uvName = fmt === 'fbx' ? activeUv : 'UVMap';
    validationFormats[fmt] = {
      reopen_ok: true,
      mesh_count: 1,
      faces: 12152,
      vertices: 6562,
      uv_layers: [uvName],
      has_uv: true,
      has_normals: !!options.include_normals,
      warnings: fmt === 'fbx' ? [] : [`${fmt}: exported UV layer named 'UVMap', expected '${activeUv}'`],
    };
  }
  const validationReport = { schema_version: 1, status: ExportRunStatus.Accepted, formats: validationFormats };
  writeFileSync(join(dir, 'validation_report.json'), JSON.stringify(validationReport, null, 2));

  const files: Record<string, string> = {
    ...nameFiles,
    uv_layout: 'uv_layout.png',
    checker_front: 'checker_front.png',
    checker_side: 'checker_side.png',
  };
  const manifest = {
    schema_version: 1,
    export_id: exportId,
    created_at: new Date().toISOString(),
    status: ExportRunStatus.Accepted,
    formats,
    options: {
      selected_uv_layer: options.selected_uv_layer ?? null,
      apply_scale: !!options.apply_scale,
      include_materials: !!options.include_materials,
      include_normals: !!options.include_normals,
      copy_textures: !!options.copy_textures,
      triangulate: !!options.triangulate,
    },
    source: {
      selected_uv_model: job.selected_uv_model_rel ?? SELECTED_UV_BLEND_REL,
      selected_uv_summary: job.selected_uv_summary_rel ?? SELECTED_UV_SUMMARY_REL,
      uv_generate_run_id: job.uv_generate_run_id ?? null,
      active_user_seam_spec: job.seam_spec_rel ?? null,
      candidate_summary: job.candidate_summary_rel ?? null,
      p5_gate: job.p5_gate_rel ?? null,
      seam_report: job.seam_report_rel ?? null,
      ai_review_run_id: null,
      ai_review_skipped: true,
    },
    metrics,
    files,
    validation: 'validation_report.json',
  };
  writeFileSync(join(dir, 'export_manifest.json'), JSON.stringify(manifest, null, 2));

  const exports: Record<string, string> = {};
  for (const fmt of formats) exports[fmt] = join('exports', exportId, nameFiles[fmt]);
  const artifacts = {
    manifest: 'export_manifest.json',
    validation_report: 'validation_report.json',
    uv_layout: 'uv_layout.png',
    checker_front: 'checker_front.png',
    checker_side: 'checker_side.png',
  };
  const result = {
    schema_version: 1,
    export_id: exportId,
    command: ExportCommand.ExportProductionAsset,
    status: ExportRunStatus.Accepted,
    source: {
      selected_uv_model: job.selected_uv_model_rel ?? SELECTED_UV_BLEND_REL,
      selected_uv_summary: job.selected_uv_summary_rel ?? SELECTED_UV_SUMMARY_REL,
      uv_generate_run_id: job.uv_generate_run_id ?? null,
      seam_spec: job.seam_spec_rel ?? null,
      selected_candidate_id: selectedCandidateId,
      ai_review_run_id: null,
      ai_review_skipped: true,
    },
    exports,
    validation: validationReport,
    artifacts,
    warnings: ['mock export: not a real Blender run'],
  };
  writeFileSync(join(dir, 'export_result.json'), JSON.stringify(result, null, 2));

  writeFileSync(
    join(dir, 'status.json'),
    JSON.stringify(
      {
        schema_version: 1,
        export_id: exportId,
        command: ExportCommand.ExportProductionAsset,
        status: ExportRunStatus.Accepted,
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        input: statusInput(formats),
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
