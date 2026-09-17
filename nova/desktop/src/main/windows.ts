/**
 * Creación de las dos ventanas.
 *
 * El overlay es una ventana transparente sin marco, siempre encima y fuera de la
 * barra de tareas, dimensionada casi exactamente al avatar para que el área
 * muerta sea mínima. En Windows, `type: 'toolbar'` evita que robe el foco al
 * aparecer, que es lo que haría que perdieras lo que estabas escribiendo.
 *
 * Posición: por defecto el avatar se ancla a una esquina del área de trabajo
 * (no de la pantalla, así nunca queda debajo de la barra de tareas). Si lo
 * arrastras, esas coordenadas mandan hasta que vuelvas a elegir una esquina
 * desde la bandeja. El anclaje se recalcula cuando cambia la resolución o
 * conectas un monitor, que es cuando unas coordenadas absolutas guardadas se
 * quedarían fuera de la pantalla.
 */
import { BrowserWindow, screen } from 'electron';
import { join } from 'node:path';

import { loadPlacement, savePlacement, type OverlayCorner, type OverlayPlacement } from './window-store';

/** El retrato es vertical (360x469), así que la ventana también lo es. */
const OVERLAY_WIDTH = 300;
const OVERLAY_HEIGHT = 420;
const MARGIN = 24;

export function rendererPath(area: 'overlay' | 'settings', root: string): string {
  return join(root, 'desktop', 'src', 'renderer', area, 'index.html');
}

/** Coordenadas de la esquina pedida dentro del área de trabajo del monitor principal. */
export function cornerPosition(corner: OverlayCorner): { x: number; y: number } {
  const { workArea } = screen.getPrimaryDisplay();
  const right = workArea.x + workArea.width - OVERLAY_WIDTH - MARGIN;
  const bottom = workArea.y + workArea.height - OVERLAY_HEIGHT - MARGIN;
  const left = workArea.x + MARGIN;
  const top = workArea.y + MARGIN;

  switch (corner) {
    case 'top-left':
      return { x: left, y: top };
    case 'top-right':
      return { x: right, y: top };
    case 'bottom-left':
      return { x: left, y: bottom };
    default:
      return { x: right, y: bottom };
  }
}

/** Recoloca el avatar en una esquina y lo recuerda, descartando el arrastre previo. */
export function anchorToCorner(window: BrowserWindow, corner: OverlayCorner): void {
  if (window.isDestroyed()) return;
  const { x, y } = cornerPosition(corner);
  window.setPosition(Math.round(x), Math.round(y), false);
  savePlacement({ corner });
}

/** Devuelve el avatar a la pantalla si unas coordenadas guardadas quedaron fuera. */
export function ensureOnScreen(window: BrowserWindow, placement: OverlayPlacement): void {
  if (window.isDestroyed()) return;
  // getPosition() devuelve number[]: con noUncheckedIndexedAccess sus elementos
  // son number | undefined, así que se desestructura con valores por defecto.
  const [x = 0, y = 0] = window.getPosition();
  const { workArea } = screen.getPrimaryDisplay();
  const visible =
    x + OVERLAY_WIDTH > workArea.x + 40 &&
    x < workArea.x + workArea.width - 40 &&
    y + OVERLAY_HEIGHT > workArea.y + 40 &&
    y < workArea.y + workArea.height - 40;
  if (!visible) anchorToCorner(window, placement.corner);
}

export function createOverlay(root: string, preload: string): BrowserWindow {
  const placement = loadPlacement();
  const anchored = cornerPosition(placement.corner);

  const window = new BrowserWindow({
    width: OVERLAY_WIDTH,
    height: OVERLAY_HEIGHT,
    x: Math.round(placement.x ?? anchored.x),
    y: Math.round(placement.y ?? anchored.y),
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    resizable: false,
    movable: true,
    skipTaskbar: true,
    focusable: true,
    alwaysOnTop: true,
    hasShadow: false,
    show: false,
    type: process.platform === 'win32' ? 'toolbar' : undefined,
    webPreferences: { preload, contextIsolation: true, nodeIntegration: false, backgroundThrottling: false },
  });

  window.setAlwaysOnTop(true, 'screen-saver');
  window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  window.loadFile(rendererPath('overlay', root));

  let saveTimer: NodeJS.Timeout | null = null;
  window.on('moved', () => {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      const [x = 0, y = 0] = window.getPosition();
      savePlacement({ corner: loadPlacement().corner, x, y });
    }, 400);
  });

  // Cambiar de resolución o desconectar un monitor deja las coordenadas
  // absolutas apuntando a ninguna parte; volver a la esquina es lo correcto.
  const reflow = (): void => ensureOnScreen(window, loadPlacement());
  screen.on('display-metrics-changed', reflow);
  screen.on('display-removed', reflow);
  window.on('closed', () => {
    screen.removeListener('display-metrics-changed', reflow);
    screen.removeListener('display-removed', reflow);
  });

  return window;
}

export function createSettings(root: string, preload: string): BrowserWindow {
  const window = new BrowserWindow({
    width: 940,
    height: 680,
    minWidth: 760,
    minHeight: 560,
    show: false,
    backgroundColor: '#060A10',
    title: 'Nova',
    autoHideMenuBar: true,
    webPreferences: { preload, contextIsolation: true, nodeIntegration: false },
  });
  window.loadFile(rendererPath('settings', root));
  window.on('close', (event) => {
    event.preventDefault();
    window.hide();
  });
  return window;
}
