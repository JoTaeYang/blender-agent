/**
 * Electron main entry (plan §3 Electron Main).
 *
 * Owns the BrowserWindow, registers IPC, and serves run-folder preview images to
 * the renderer over a custom `uvpreview://` protocol (sandboxed file access).
 */

import { app, BrowserWindow, protocol, net, nativeImage } from 'electron';
import { join } from 'path';
import { existsSync } from 'fs';
import { pathToFileURL } from 'url';
import { registerIpc } from './ipc';

// Set the product name early so the macOS dock tooltip + app menu read "Reforge"
// instead of the dev-mode "Electron" binary name (must run before app `ready`).
app.setName('Reforge');

const isDev = !!process.env['ELECTRON_RENDERER_URL'];

/** Resolve the Reforge app icon (app/resources/icon.png in dev, bundled resources
 *  when packaged). Returns null if missing so window creation never fails on it. */
function loadAppIcon(): Electron.NativeImage | null {
  const candidates = app.isPackaged
    ? [join(process.resourcesPath, 'icon.png')]
    : [join(__dirname, '../../resources/icon.png')];
  for (const p of candidates) {
    if (existsSync(p)) {
      const img = nativeImage.createFromPath(p);
      if (!img.isEmpty()) return img;
    }
  }
  return null;
}

function createWindow(): void {
  const icon = loadAppIcon();
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    title: 'Reforge',
    ...(icon ? { icon } : {}),
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  if (isDev) {
    win.loadURL(process.env['ELECTRON_RENDERER_URL'] as string);
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'));
  }
}

app.whenReady().then(() => {
  // Serve arbitrary local preview PNGs the renderer references by absolute path.
  protocol.handle('uvpreview', (request) => {
    const filePath = decodeURIComponent(request.url.replace('uvpreview://', ''));
    return net.fetch(pathToFileURL(filePath).toString());
  });

  // macOS shows the dock icon (BrowserWindow `icon` is ignored there).
  if (process.platform === 'darwin' && app.dock) {
    const icon = loadAppIcon();
    if (icon) app.dock.setIcon(icon);
  }

  registerIpc();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
