/**
 * Main-process integration smoke (plan Session C / E acceptance).
 *
 * Exercises the full create -> inspect -> generate -> approve flow against the
 * mock worker runner — no Electron window, no Blender. Verifies the project
 * manifest, status lifecycle, and summary contract the next MVP will read.
 *
 * Run: npm run test:integration
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, existsSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  createProject,
  openProject,
  getRunView,
  approveLowpoly,
  registerRun,
  absSourcePath,
} from '../electron/main/project-service';
import { WorkerRunner } from '../electron/main/worker-runner';

function makeFakeSource(): string {
  const dir = mkdtempSync(join(tmpdir(), 'uvsrc-'));
  const src = join(dir, 'SM_Test_Pottery_a_02.fbx');
  writeFileSync(src, 'FAKE-FBX-CONTENT');
  return src;
}

function workerRoot(): string {
  // app/test -> app -> repo -> worker
  return join(__dirname, '..', '..', 'worker');
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

test('create -> inspect -> generate(mock) -> approve', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();

  // 1. create project
  const project = createProject({ root, name: 'pottery_test', sourcePath, role: 'highpoly' });
  assert.equal(project.schema_version, 1);
  assert.ok(project.dir && existsSync(project.dir));
  assert.ok(existsSync(join(project.dir!, 'source', 'original.fbx')));
  assert.equal(project.source_model_role, 'highpoly');

  const runner = new WorkerRunner({ blenderPath: null, workerRoot: workerRoot(), mock: true });

  // 2. inspect (mock)
  const inspect = await runner.inspect(project.id, project.dir!, absSourcePath(project));
  assert.equal(inspect.status, 'accepted');
  assert.ok(inspect.objects && inspect.objects.length > 0);
  const objName = inspect.objects![0].name;

  // 3. generate (mock, async)
  const { run_id } = runner.generate(project.id, project.dir!, {
    sourceModel: absSourcePath(project),
    objectName: objName,
    targetFaces: 12000,
  });
  assert.ok(run_id.startsWith('run_'));

  // wait for the mock run to finish writing artifacts
  let view = getRunView(project.dir!, run_id);
  for (let i = 0; i < 50 && view.status?.status !== 'accepted'; i++) {
    await sleep(20);
    view = getRunView(project.dir!, run_id);
  }
  assert.equal(view.status?.status, 'accepted');
  assert.ok(view.summary, 'summary.json exists');
  assert.equal(view.summary!.metrics.target_faces, 12000);
  assert.ok((view.summary!.metrics.actual_faces ?? 0) > 0, 'actual faces present');
  assert.ok(view.preview_path && existsSync(view.preview_path), 'preview image artifact');
  assert.ok(existsSync(join(view.dir, 'status.json')));

  // 4. approve
  const approve = approveLowpoly(project.dir!, run_id);
  assert.equal(approve.status, 'accepted');
  assert.equal(approve.approved_lowpoly_run_id, run_id);
  assert.ok(existsSync(join(project.dir!, 'work', 'working_lowpoly.blend')));

  // 5. manifest points at the working model for the next MVP
  const reopened = openProject(project.dir!);
  assert.equal(reopened.approved_lowpoly_run_id, run_id);
  assert.equal(reopened.working_model, join('work', 'working_lowpoly.blend'));
  assert.ok(reopened.runs.includes(run_id));
});

test('failed run does not crash and leaves a failed status', async () => {
  const root = mkdtempSync(join(tmpdir(), 'uvproj-'));
  const sourcePath = makeFakeSource();
  const project = createProject({ root, name: 'fail_test', sourcePath });
  // Non-mock runner with a Blender path that does not exist -> spawn fails.
  const runner = new WorkerRunner({
    blenderPath: '/nonexistent/blender',
    workerRoot: workerRoot(),
  });
  const { run_id } = runner.generate(project.id, project.dir!, {
    sourceModel: absSourcePath(project),
    objectName: 'X',
    targetFaces: 8000,
  });
  registerRun(project.dir!, run_id);
  let view = getRunView(project.dir!, run_id);
  for (let i = 0; i < 50 && (!view.status || view.status.status === 'queued'); i++) {
    await sleep(20);
    view = getRunView(project.dir!, run_id);
  }
  assert.equal(view.status?.status, 'failed');
  assert.ok(view.status?.error);
  // The raw status.json file is on disk (UI never parses stdout).
  const onDisk = JSON.parse(readFileSync(join(view.dir, 'status.json'), 'utf-8'));
  assert.equal(onDisk.status, 'failed');
});
