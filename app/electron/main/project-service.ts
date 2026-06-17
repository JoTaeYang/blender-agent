/**
 * Project folder lifecycle (plan §4 Project Folder Contract).
 *
 * Pure Node (fs/path/crypto) — no `electron` import — so the main-process logic
 * is unit-testable without a renderer or a running app (Session C acceptance).
 */

import { randomUUID } from 'crypto';
import { existsSync, mkdirSync, copyFileSync, readFileSync, writeFileSync, readdirSync } from 'fs';
import { extname, join } from 'path';
import {
  SCHEMA_VERSION,
  type MeshRole,
  type Project,
  type RunStatusDoc,
  type Summary,
  type RunView,
} from '@shared/contracts';

function nowIso(): string {
  return new Date().toISOString();
}

export function projectJsonPath(projectDir: string): string {
  return join(projectDir, 'project.json');
}

export function readProject(projectDir: string): Project {
  const raw = JSON.parse(readFileSync(projectJsonPath(projectDir), 'utf-8')) as Project;
  raw.dir = projectDir;
  return raw;
}

export function writeProject(projectDir: string, project: Project): Project {
  const { dir, ...body } = project;
  void dir; // `dir` is runtime-only, never persisted into the file body.
  body.updated_at = nowIso();
  writeFileSync(projectJsonPath(projectDir), JSON.stringify(body, null, 2));
  return { ...body, dir: projectDir };
}

/** Create `<root>/<name>/` with the project folder skeleton and copy the source. */
export function createProject(opts: {
  root: string;
  name: string;
  sourcePath: string;
  role?: MeshRole;
}): Project {
  const { root, name, sourcePath, role } = opts;
  if (!existsSync(sourcePath)) {
    throw new Error(`source model not found: ${sourcePath}`);
  }
  const safeName = name.trim().replace(/[^\w.-]+/g, '_') || 'project';
  let projectDir = join(root, safeName);
  let suffix = 1;
  while (existsSync(projectDir)) {
    projectDir = join(root, `${safeName}_${suffix++}`);
  }
  for (const sub of ['source', 'work', 'runs', 'previews', 'reports']) {
    mkdirSync(join(projectDir, sub), { recursive: true });
  }

  const ext = extname(sourcePath).toLowerCase();
  const sourceRel = join('source', `original${ext}`);
  copyFileSync(sourcePath, join(projectDir, sourceRel));

  const project: Project = {
    schema_version: SCHEMA_VERSION,
    id: `project_${randomUUID()}`,
    name,
    created_at: nowIso(),
    updated_at: nowIso(),
    source_model: sourceRel,
    source_model_role: role ?? null,
    selected_object: null,
    working_model: null,
    working_model_fbx: null,
    approved_lowpoly_run_id: null,
    runs: [],
  };
  return writeProject(projectDir, project);
}

export function openProject(projectDir: string): Project {
  if (!existsSync(projectJsonPath(projectDir))) {
    throw new Error(`not a project folder (no project.json): ${projectDir}`);
  }
  return readProject(projectDir);
}

export function absSourcePath(project: Project): string {
  if (!project.dir || !project.source_model) {
    throw new Error('project has no source model');
  }
  return join(project.dir, project.source_model);
}

export function newRunId(): string {
  return `run_${randomUUID()}`;
}

export function runDir(projectDir: string, runId: string): string {
  return join(projectDir, 'runs', runId);
}

export function ensureRunDir(projectDir: string, runId: string): string {
  const dir = runDir(projectDir, runId);
  mkdirSync(dir, { recursive: true });
  return dir;
}

export function registerRun(projectDir: string, runId: string): Project {
  const project = readProject(projectDir);
  if (!project.runs.includes(runId)) {
    project.runs.push(runId);
  }
  return writeProject(projectDir, project);
}

function readJsonIfExists<T>(path: string): T | null {
  try {
    return JSON.parse(readFileSync(path, 'utf-8')) as T;
  } catch {
    return null;
  }
}

function readTextIfExists(path: string): string {
  try {
    return readFileSync(path, 'utf-8');
  } catch {
    return '';
  }
}

/** Assemble the combined run view the renderer polls (plan §3). */
export function getRunView(projectDir: string, runId: string): RunView {
  const dir = runDir(projectDir, runId);
  const reportNames = [
    'generation_report',
    'validation_report',
    'shape_report',
    'quadflow_report',
    'feature_report',
    'retopo_plan',
  ];
  const reports: Record<string, unknown> = {};
  for (const n of reportNames) {
    const r = readJsonIfExists<unknown>(join(dir, `${n}.json`));
    if (r !== null) reports[n] = r;
  }
  const previewAbs = join(dir, 'preview.png');
  return {
    run_id: runId,
    dir,
    status: readJsonIfExists<RunStatusDoc>(join(dir, 'status.json')),
    summary: readJsonIfExists<Summary>(join(dir, 'summary.json')),
    reports,
    stdout: readTextIfExists(join(dir, 'stdout.log')),
    stderr: readTextIfExists(join(dir, 'stderr.log')),
    preview_path: existsSync(previewAbs) ? previewAbs : null,
  };
}

export function listRuns(projectDir: string): string[] {
  const runsRoot = join(projectDir, 'runs');
  if (!existsSync(runsRoot)) return [];
  return readdirSync(runsRoot, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name);
}

/** Approve a run: copy its low-poly into `work/` and update the manifest (plan §5.3). */
export function approveLowpoly(projectDir: string, runId: string): {
  status: string;
  working_model: string;
  working_model_fbx: string | null;
  approved_lowpoly_run_id: string;
} {
  const dir = runDir(projectDir, runId);
  const srcBlend = join(dir, 'lowpoly.blend');
  if (!existsSync(srcBlend)) {
    throw new Error(`run ${runId} has no lowpoly.blend to approve`);
  }
  const workBlendRel = join('work', 'working_lowpoly.blend');
  copyFileSync(srcBlend, join(projectDir, workBlendRel));

  let workFbxRel: string | null = null;
  const srcFbx = join(dir, 'lowpoly.fbx');
  if (existsSync(srcFbx)) {
    workFbxRel = join('work', 'working_lowpoly.fbx');
    copyFileSync(srcFbx, join(projectDir, workFbxRel));
  }

  const project = readProject(projectDir);
  project.working_model = workBlendRel;
  project.working_model_fbx = workFbxRel;
  project.approved_lowpoly_run_id = runId;
  writeProject(projectDir, project);

  return {
    status: 'accepted',
    working_model: workBlendRel,
    working_model_fbx: workFbxRel,
    approved_lowpoly_run_id: runId,
  };
}
