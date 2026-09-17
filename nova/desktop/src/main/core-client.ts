/**
 * Cliente WebSocket hacia el núcleo, con reconexión.
 *
 * Los eventos se reenvían tal cual a las ventanas; el proceso principal no
 * interpreta el contenido salvo para cambiar la visibilidad del avatar.
 */
import WebSocket from 'ws';

import type { CoreEvent, UiCommand } from './protocol';

export class CoreClient {
  private socket: WebSocket | null = null;
  private timer: NodeJS.Timeout | null = null;
  private closing = false;
  private queue: UiCommand[] = [];

  constructor(
    private readonly url: string,
    private readonly onEvent: (event: CoreEvent) => void,
    private readonly onConnectionChange: (connected: boolean) => void,
  ) {}

  connect(): void {
    if (this.closing) return;
    const socket = new WebSocket(this.url);
    this.socket = socket;

    socket.on('open', () => {
      this.onConnectionChange(true);
      while (this.queue.length) {
        const command = this.queue.shift();
        if (command) socket.send(JSON.stringify(command));
      }
    });
    socket.on('message', (raw: WebSocket.RawData) => {
      try {
        this.onEvent(JSON.parse(raw.toString()) as CoreEvent);
      } catch {
        /* una trama corrupta no debe tumbar la conexión */
      }
    });
    socket.on('close', () => this.retry());
    socket.on('error', () => socket.close());
  }

  private retry(): void {
    this.socket = null;
    this.onConnectionChange(false);
    if (this.closing || this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, 1200);
  }

  send(command: UiCommand): void {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(command));
    else if (this.queue.length < 20) this.queue.push(command);
  }

  close(): void {
    this.closing = true;
    if (this.timer) clearTimeout(this.timer);
    this.socket?.close();
  }
}
