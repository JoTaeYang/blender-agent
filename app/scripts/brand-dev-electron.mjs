/**
 * Dev-only branding: rename the Electron dev bundle so the dock tooltip + app
 * menu read "Reforge" instead of "Electron".
 *
 * In `electron-vite dev` the app runs from node_modules/electron's Electron.app,
 * whose Info.plist CFBundleName is "Electron" — and the macOS dock shows THAT, not
 * `app.setName()`. This patches CFBundleName/CFBundleDisplayName in place.
 *
 * Idempotent and best-effort: it no-ops off macOS, when the bundle is missing, or
 * when already branded. node_modules is wiped on reinstall, so this runs from the
 * `predev` hook to reapply automatically. The permanent fix for shipped builds is
 * electron-builder `productName: "Reforge"` (produces a real Reforge.app).
 */
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const NAME = 'Reforge';
// A UNIQUE bundle id is essential: every workspace's node_modules ships an
// Electron.app with id "com.github.Electron" + name "Electron". macOS
// LaunchServices caches names keyed by bundle id, so it would resolve the shared
// "Electron" name no matter what CFBundleName this copy sets. A distinct id makes
// this bundle a separate app in the LS database, so its "Reforge" name sticks.
const BUNDLE_ID = 'co.thakicloud.reforge.dev';

if (process.platform !== 'darwin') {
  process.exit(0); // dock-name issue is macOS-specific
}

const electronPackageDir = join(process.cwd(), 'node_modules', 'electron');
const distDir = join(electronPackageDir, 'dist');
const appBundle = join(distDir, 'Electron.app');
const plist = join(appBundle, 'Contents', 'Info.plist');
const macOsDir = join(appBundle, 'Contents', 'MacOS');
const electronExecutable = join(macOsDir, 'Electron');
const pathFile = join(electronPackageDir, 'path.txt');
const brandedPath = 'Electron.app/Contents/MacOS/Electron';

if (!existsSync(appBundle)) {
  console.log('[brand-dev] Electron dev bundle not found; skipping');
  process.exit(0);
}

function get(key) {
  try {
    return execFileSync('/usr/libexec/PlistBuddy', ['-c', `Print :${key}`, plist], {
      encoding: 'utf8',
    }).trim();
  } catch {
    return null;
  }
}

function set(key, value) {
  const exists = get(key) !== null;
  const cmd = exists ? `Set :${key} ${value}` : `Add :${key} string ${value}`;
  execFileSync('/usr/libexec/PlistBuddy', ['-c', cmd, plist]);
}

if (
  get('CFBundleName') === NAME &&
  get('CFBundleDisplayName') === NAME &&
  get('CFBundleIdentifier') === BUNDLE_ID &&
  get('CFBundleExecutable') === 'Electron' &&
  existsSync(electronExecutable) &&
  readElectronPath() === brandedPath
) {
  console.log(`[brand-dev] already "${NAME}"`);
  process.exit(0);
}

function readElectronPath() {
  try {
    return readFileSync(pathFile, 'utf8');
  } catch {
    return null;
  }
}

try {
  set('CFBundleName', NAME);
  set('CFBundleDisplayName', NAME);
  set('CFBundleIdentifier', BUNDLE_ID);
  set('CFBundleExecutable', 'Electron');
  if (existsSync(pathFile)) {
    // electron/index.js reads path.txt verbatim, so this file must not end with
    // a newline; otherwise spawn receives a path ending in "\n".
    writeFileSync(pathFile, brandedPath);
  }
  // Refresh the macOS LaunchServices cache so the dock/menu read the new name
  // without a logout (touch + force re-register the bundle). Best-effort.
  try {
    execFileSync('/usr/bin/touch', [appBundle]);
  } catch {
    /* non-fatal */
  }
  try {
    execFileSync(
      '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister',
      ['-f', appBundle],
    );
  } catch {
    /* lsregister path varies / non-fatal */
  }
  // Restart the Dock so it drops the stale "Electron" name cache. Harmless (the
  // Dock relaunches instantly). Only reached when a rename actually happened.
  try {
    execFileSync('/usr/bin/killall', ['Dock']);
  } catch {
    /* non-fatal */
  }
  console.log(`[brand-dev] Electron dev bundle -> "${NAME}" (id ${BUNDLE_ID})`);
} catch (err) {
  console.log('[brand-dev] could not patch Info.plist (non-fatal):', String(err));
}
