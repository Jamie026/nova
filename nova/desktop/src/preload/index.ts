/**
 * Puente aislado hacia el renderer. Superficie mínima: enviar comandos y
 * suscribirse a eventos. El renderer nunca ve Node ni el WebSocket.
 */
import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron';

export interface LogEntry {
  line: string;
  stream: 'out' | 'err';
}

export interface NovaBridge {
  send: (command: unknown) => void;
  onEvent: (handler: (event: Record<string, unknown>) => void) => () => void;
  onLog: (handler: (entry: LogEntry) => void) => () => void;
  onConnection: (handler: (info: { connected: boolean }) => void) => () => void;
  openSettings: () => void;
  hideOverlay: () => void;
  env: () => Promise<{ isDev: boolean; mock: boolean; port: number }>;
}

function subscribe<T>(channel: string, handler: (payload: T) => void): () => void {
  const listener = (_event: IpcRendererEvent, payload: T): void => handler(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

const bridge: NovaBridge = {
  send: (command) => ipcRenderer.send('ui:command', command),
  onEvent: (handler) => subscribe('core:event', handler),
  onLog: (handler) => subscribe('core:log', handler),
  onConnection: (handler) => subscribe('core:connection', handler),
  openSettings: () => ipcRenderer.send('ui:open-settings'),
  hideOverlay: () => ipcRenderer.send('ui:hide-overlay'),
  env: () => ipcRenderer.invoke('ui:env'),
};

contextBridge.exposeInMainWorld('nova', bridge);
