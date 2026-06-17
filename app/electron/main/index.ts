/**
 * Electron main entry (plan §3 Electron Main).
 *
 * Owns the BrowserWindow, registers IPC, and serves run-folder preview images to
 * the renderer over a custom `uvpreview://` protocol (sandboxed file access).
 */

import { app, BrowserWindow, protocol, net } from 'electron';
import { join } from 'path';
import { pathToFileURL } from 'url';
import { registerIpc } from './ipc';

const isDev = !!process.env['ELECTRON_RENDERER_URL'];

function createWindow(): void {
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    title: 'UV Review App — MVP 0',
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

  registerIpc();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
