/**
 * Context-isolated preload bridge (plan §3: renderer never calls Blender/fs).
 *
 * Exposes a typed `window.api` matching `RendererApi`; every method is a thin
 * `ipcRenderer.invoke` over the channels in `@shared/contracts`.
 */

import { contextBridge, ipcRenderer } from 'electron';
import { Ipc, type RendererApi } from '@shared/contracts';

const api: RendererApi = {
  projectCreate: (input) => ipcRenderer.invoke(Ipc.ProjectCreate, input),
  projectOpen: (dir) => ipcRenderer.invoke(Ipc.ProjectOpen, dir),
  projectGet: (projectId) => ipcRenderer.invoke(Ipc.ProjectGet, projectId),
  modelInspect: (input) => ipcRenderer.invoke(Ipc.ModelInspect, input),
  lowpolyGenerate: (input) => ipcRenderer.invoke(Ipc.LowpolyGenerate, input),
  lowpolyApprove: (input) => ipcRenderer.invoke(Ipc.LowpolyApprove, input),
  runGet: (input) => ipcRenderer.invoke(Ipc.RunGet, input),
  runList: (projectId) => ipcRenderer.invoke(Ipc.RunList, projectId),
  settingsGet: () => ipcRenderer.invoke(Ipc.SettingsGet),
  settingsSet: (patch) => ipcRenderer.invoke(Ipc.SettingsSet, patch),
  pickFile: () => ipcRenderer.invoke(Ipc.PickFile),
  pickProjectDir: () => ipcRenderer.invoke(Ipc.PickProjectDir),
  onRunUpdate: (cb) => {
    const listener = (_e: unknown, payload: { projectId: string; runId: string }) => cb(payload);
    ipcRenderer.on(Ipc.RunUpdate, listener);
    return () => ipcRenderer.removeListener(Ipc.RunUpdate, listener);
  },
};

contextBridge.exposeInMainWorld('api', api);
