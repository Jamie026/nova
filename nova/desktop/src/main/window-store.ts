/** Persistencia mínima de la posición del avatar. Un JSON basta; no hace falta una dependencia. */
import { app } from 'electron';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

export type OverlayCorner = 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right';

export const CORNERS: OverlayCorner[] = ['top-left', 'top-right', 'bottom-left', 'bottom-right'];

export interface OverlayPlacement {
  /** Esquina a la que se ancla el avatar. Se recalcula al cambiar de pantalla. */
  corner: OverlayCorner;
  /** Coordenadas libres: sólo existen si el usuario arrastró el avatar. */
  x?: number;
  y?: number;
}

const DEFAULT_PLACEMENT: OverlayPlacement = { corner: 'bottom-right' };

const file = (): string => join(app.getPath('userData'), 'window-state.json');

function isCorner(value: unknown): value is OverlayCorner {
  return typeof value === 'string' && (CORNERS as string[]).includes(value);
}

export function loadPlacement(): OverlayPlacement {
  try {
    const path = file();
    if (!existsSync(path)) return { ...DEFAULT_PLACEMENT };
    const data = JSON.parse(readFileSync(path, 'utf8')) as Partial<OverlayPlacement>;
    const placement: OverlayPlacement = {
      corner: isCorner(data.corner) ? data.corner : DEFAULT_PLACEMENT.corner,
    };
    if (typeof data.x === 'number' && typeof data.y === 'number') {
      placement.x = data.x;
      placement.y = data.y;
    }
    return placement;
  } catch {
    /* posición corrupta: se recalcula por defecto */
  }
  return { ...DEFAULT_PLACEMENT };
}

export function savePlacement(placement: OverlayPlacement): void {
  try {
    mkdirSync(dirname(file()), { recursive: true });
    writeFileSync(file(), JSON.stringify(placement), 'utf8');
  } catch {
    /* no poder guardar la posición nunca debe romper la app */
  }
}
