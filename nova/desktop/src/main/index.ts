/**
 * Proceso principal: ciclo de vida, ventanas, bandeja y puente con el núcleo.
 *
 * Toda la lógica del agente vive en Python. Aquí sólo se decide cuándo se ve el
 * avatar y por dónde viajan los mensajes.
 */
import { app, BrowserWindow, dialog, globalShortcut, ipcMain, Menu, nativeImage, Tray } from 'electron';
import { config as loadEnv } from 'dotenv';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

import { CoreClient } from './core-client';
import { CoreProcess } from './core-process';
import type { CoreEvent, UiCommand } from './protocol';
import { TRAY_ICON_DATA_URL } from './tray-icon';
import { CORNERS, loadPlacement, type OverlayCorner } from './window-store';
import { anchorToCorner, createOverlay, createSettings } from './windows';

// Dos raíces, porque instalado y en desarrollo no son la misma carpeta:
//  - appRoot: la interfaz (dist/ y desktop/src/renderer). Instalado va dentro
//    del app.asar, que Electron lee sin problema.
//  - coreRoot: el núcleo Python. Instalado va en resources/core, FUERA del
//    asar, porque Python no puede ejecutar nada desde dentro de uno.
// En desarrollo las dos son la raíz del repo.
const appRoot = app.isPackaged ? app.getAppPath() : resolve(__dirname, '..', '..');
const coreRoot = app.isPackaged ? process.resourcesPath : appRoot;
// El .env: en el repo, junto al package.json; instalado, en %APPDATA%/Nova,
// que es donde el núcleo guarda el resto de su configuración.
const envFile = app.isPackaged
  ? join(app.getPath('appData'), 'Nova', '.env')
  : join(appRoot, '.env');
const preload = join(__dirname, '..', 'preload', 'index.js');
const isDev = process.argv.includes('--dev');
const mock = process.env.NOVA_MOCK === '1';
// Con NOVA_NO_CORE=1 la interfaz no lanza su propio núcleo y se limita a
// conectarse a uno ya en marcha. Es lo que permite depurar los dos procesos a la
// vez desde VS Code sin que dos núcleos peleen por el micrófono y por el puerto.
const manageCore = process.env.NOVA_NO_CORE !== '1';
const port = Number(process.env.NOVA_IPC_PORT ?? 8765);

let overlay: BrowserWindow | null = null;
let settings: BrowserWindow | null = null;
let tray: Tray | null = null;
let core: CoreProcess | null = null;
let client: CoreClient | null = null;

/** Estados en los que el avatar debe estar a la vista. */
const VISIBLE_STATES = new Set(['WAKE_WORD_DETECTED', 'LISTENING', 'THINKING', 'EXECUTING', 'SPEAKING', 'ERROR']);

/** Retardo antes de ocultar: evita parpadeos entre estados encadenados. */
const HIDE_DELAY_MS = 900;

let lastState = 'IDLE';

function broadcast(channel: string, payload: unknown): void {
  for (const window of [overlay, settings]) {
    if (window && !window.isDestroyed()) window.webContents.send(channel, payload);
  }
}

function syncOverlayVisibility(state: string): void {
  if (!overlay || overlay.isDestroyed()) return;
  if (VISIBLE_STATES.has(state)) {
    // showInactive y no show: el avatar no debe robarte el foco mientras escribes.
    if (!overlay.isVisible()) overlay.showInactive();
    return;
  }
  setTimeout(() => {
    if (overlay && !overlay.isDestroyed() && !VISIBLE_STATES.has(lastState)) overlay.hide();
  }, HIDE_DELAY_MS);
}

function handleCoreEvent(event: CoreEvent): void {
  if (event.type === 'state') {
    lastState = event.state ?? 'IDLE';
    syncOverlayVisibility(lastState);
  }
  if (event.type === 'ready') core?.noteHealthy();
  broadcast('core:event', event);
}

const CORNER_LABEL: Record<OverlayCorner, string> = {
  'top-left': 'Arriba a la izquierda',
  'top-right': 'Arriba a la derecha',
  'bottom-left': 'Abajo a la izquierda',
  'bottom-right': 'Abajo a la derecha',
};

function buildTray(): void {
  const icon = nativeImage.createFromDataURL(TRAY_ICON_DATA_URL);
  const current = loadPlacement().corner;
  tray = new Tray(icon);
  tray.setToolTip('Nova');
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Despertar a Nova', click: () => client?.send({ type: 'wake' }) },
      { label: 'Ajustes', click: () => settings?.show() },
      {
        label: 'Esquina del avatar',
        submenu: CORNERS.map((corner) => ({
          label: CORNER_LABEL[corner],
          type: 'radio' as const,
          checked: corner === current,
          click: () => {
            if (overlay) anchorToCorner(overlay, corner);
          },
        })),
      },
      { type: 'separator' },
      {
        label: 'Iniciar con Windows',
        type: 'checkbox',
        checked: app.getLoginItemSettings().openAtLogin,
        click: (item) => app.setLoginItemSettings({ openAtLogin: item.checked }),
      },
      { type: 'separator' },
      { label: 'Salir', click: () => shutdown() },
    ]),
  );
  tray.on('double-click', () => settings?.show());
}

function registerIpc(): void {
  ipcMain.on('ui:command', (_event, command: UiCommand) => client?.send(command));
  ipcMain.on('ui:open-settings', () => settings?.show());
  ipcMain.on('ui:hide-overlay', () => overlay?.hide());
  ipcMain.handle('ui:env', () => ({ isDev, mock, port }));
}

function shutdown(): void {
  core?.stop();
  client?.close();
  overlay?.destroy();
  settings?.destroy();
  app.exit(0);
}

/**
 * Instalado, la primera vez no hay venv: el instalador no lo lleva (5 GB y un
 * venv de Windows no se puede copiar entre máquinas). Se crea con un script en
 * una ventana visible, para que se vea qué está pasando, y al terminar arranca
 * el núcleo. Devuelve false si el usuario canceló o falló.
 */
async function ensureCoreEnvironment(): Promise<boolean> {
  const venv = join(coreRoot, 'core', '.venv', 'Scripts', 'python.exe');
  if (!app.isPackaged || process.platform !== 'win32' || existsSync(venv)) return true;
  const script = join(coreRoot, 'scripts', 'instalar-nucleo.ps1');
  const { response } = await dialog.showMessageBox({
    type: 'info',
    title: 'Nova — primer arranque',
    message: 'Hace falta preparar el núcleo (una sola vez).',
    detail:
      'Se instalarán las dependencias de Python en la carpeta de Nova. Hace falta tener ' +
      'Python 3.13 instalado y conexión a internet. Tarda unos minutos.\n\n' +
      '¿Instalar también la voz clonada (Chatterbox, ~4 GB más)? Sin ella Nova habla con la voz de Microsoft (edge-tts).',
    buttons: ['Instalar con voz clonada', 'Instalar sin voz clonada', 'Cancelar'],
    defaultId: 0,
    cancelId: 2,
  });
  if (response === 2) return false;
  const args = ['-ExecutionPolicy', 'Bypass', '-File', script];
  if (response === 0) args.push('-Voz');
  const code = await new Promise<number | null>((done) => {
    const child = spawn('powershell.exe', args, { stdio: 'ignore', windowsHide: false });
    child.on('exit', done);
    child.on('error', () => done(1));
  });
  if (code !== 0 || !existsSync(venv)) {
    await dialog.showMessageBox({
      type: 'error',
      title: 'Nova',
      message: 'No se pudo preparar el núcleo.',
      detail: `Vuelve a abrir Nova para reintentarlo, o ejecuta a mano:\n${script}`,
    });
    return false;
  }
  return true;
}

app.whenReady().then(async () => {
  app.setAppUserModelId('com.nova.desktop');
  if (manageCore && !(await ensureCoreEnvironment())) {
    app.exit(1);
    return;
  }
  loadEnv({ path: envFile });

  overlay = createOverlay(appRoot, preload);
  settings = createSettings(appRoot, preload);
  buildTray();
  registerIpc();

  if (manageCore) {
    core = new CoreProcess({
      repoRoot: coreRoot,
      mock,
      onLog: (line, stream) => broadcast('core:log', { line, stream }),
      onExit: (code) => broadcast('core:log', { line: `núcleo terminado (código ${code})`, stream: 'err' }),
    });
    core.start();
  }

  client = new CoreClient(
    `ws://127.0.0.1:${port}`,
    handleCoreEvent,
    (connected) => broadcast('core:connection', { connected }),
  );
  client.connect();

  // Atajo para despertar sin voz: útil con el micro lejos o en una llamada.
  globalShortcut.register('Control+Shift+Space', () => client?.send({ type: 'wake' }));
  globalShortcut.register('Control+Shift+X', () => client?.send({ type: 'stop' }));

  if (isDev) settings.show();
});

app.on('window-all-closed', () => {
  /* Nova vive en la bandeja: cerrar ventanas no cierra la aplicación. */
});
app.on('before-quit', () => {
  core?.stop();
  client?.close();
});
app.on('will-quit', () => globalShortcut.unregisterAll());
