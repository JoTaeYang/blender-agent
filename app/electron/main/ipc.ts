/**
 * IPC handler registration (plan §5 IPC API, Session C).
 *
 * Maps the renderer-visible IPC channels onto the project service + worker runner.
 * The renderer never touches the filesystem or spawns Blender directly (plan §3).
 */

import { dialog, ipcMain, BrowserWindow } from 'electron';
import { existsSync, mkdirSync } from 'fs';
import { join, resolve } from 'path';
import {
  Ipc,
  type GenerateOptions,
  type MeshRole,
} from '@shared/contracts';
import {
  approveLowpoly,
  absSourcePath,
  createProject,
  getRunView,
  listRuns,
  openProject,
  readProject,
  writeProject,
} from './project-service';
import { WorkerRunner } from './worker-runner';
import { getSettings, setSettings } from './settings';

/** Resolve the repo `worker/` directory from the app bundle location. */
function resolveWorkerRoot(): string {
  // In dev/build the app dir is `<repo>/app`; workers live in `<repo>/worker`.
  // __dirname is `<repo>/app/out/main` (built) — walk up to the repo root.
  const candidates = [
    resolve(__dirname, '../../../worker'),
    resolve(__dirname, '../../worker'),
    resolve(process.cwd(), '../worker'),
    resolve(process.cwd(), 'worker'),
  ];
  for (const c of candidates) {
    if (existsSync(join(c, 'run_app_retopo_job.py'))) return c;
  }
  return candidates[0];
}

/** Index of created/opened projects by id -> absolute dir, for this session. */
const projectDirs = new Map<string, string>();

function dirForProject(projectId: string): string {
  const dir = projectDirs.get(projectId);
  if (!dir) throw new Error(`unknown project id: ${projectId} (open it first)`);
  return dir;
}

function makeRunner(): WorkerRunner {
  const settings = getSettings();
  return new WorkerRunner({
    blenderPath: settings.blenderPath,
    workerRoot: resolveWorkerRoot(),
    onRunUpdate: (projectId, runId) => {
      for (const win of BrowserWindow.getAllWindows()) {
        win.webContents.send(Ipc.RunUpdate, { projectId, runId });
      }
    },
  });
}

export function registerIpc(): void {
  ipcMain.handle(Ipc.SettingsGet, () => getSettings());
  ipcMain.handle(Ipc.SettingsSet, (_e, patch) => setSettings(patch));

  ipcMain.handle(Ipc.PickFile, async () => {
    const res = await dialog.showOpenDialog({
      properties: ['openFile'],
      filters: [{ name: 'Models', extensions: ['fbx', 'obj', 'glb', 'gltf'] }],
    });
    return res.canceled ? null : res.filePaths[0];
  });

  ipcMain.handle(Ipc.PickProjectDir, async () => {
    const res = await dialog.showOpenDialog({ properties: ['openDirectory'] });
    return res.canceled ? null : res.filePaths[0];
  });

  ipcMain.handle(Ipc.ProjectCreate, (_e, input: { name: string; sourcePath: string; role?: MeshRole }) => {
    const settings = getSettings();
    const root = settings.projectsRoot ?? process.cwd();
    if (!existsSync(root)) mkdirSync(root, { recursive: true });
    const project = createProject({ root, name: input.name, sourcePath: input.sourcePath, role: input.role });
    projectDirs.set(project.id, project.dir as string);
    return project;
  });

  ipcMain.handle(Ipc.ProjectOpen, (_e, dir: string) => {
    const project = openProject(dir);
    projectDirs.set(project.id, dir);
    return project;
  });

  ipcMain.handle(Ipc.ProjectGet, (_e, projectId: string) => readProject(dirForProject(projectId)));

  ipcMain.handle(Ipc.ModelInspect, async (_e, input: { projectId: string; path?: string }) => {
    const dir = dirForProject(input.projectId);
    const project = readProject(dir);
    const sourcePath = input.path ?? absSourcePath(project);
    return makeRunner().inspect(input.projectId, dir, sourcePath);
  });

  ipcMain.handle(
    Ipc.LowpolyGenerate,
    (_e, input: { projectId: string; objectName: string; targetFaces: number; options?: GenerateOptions }) => {
      const dir = dirForProject(input.projectId);
      const project = readProject(dir);
      // Persist the chosen object on the project manifest.
      if (project.selected_object !== input.objectName) {
        project.selected_object = input.objectName;
        writeProject(dir, project);
      }
      return makeRunner().generate(input.projectId, dir, {
        sourceModel: absSourcePath(project),
        objectName: input.objectName,
        targetFaces: input.targetFaces,
        options: input.options,
      });
    },
  );

  ipcMain.handle(Ipc.LowpolyApprove, (_e, input: { projectId: string; runId: string }) =>
    approveLowpoly(dirForProject(input.projectId), input.runId),
  );

  ipcMain.handle(Ipc.RunGet, (_e, input: { projectId: string; runId: string }) =>
    getRunView(dirForProject(input.projectId), input.runId),
  );

  ipcMain.handle(Ipc.RunList, (_e, projectId: string) => listRuns(dirForProject(projectId)));
}
