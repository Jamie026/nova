/**
 * Arranque y supervisión del núcleo Python.
 *
 * El núcleo corre como proceso hijo y se reinicia solo si muere: un fallo del
 * motor de voz no debe tumbar la interfaz ni obligar a reiniciar todo, que es
 * justo lo que pide el requisito de recuperación ante errores.
 */
import { ChildProcess, spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

export interface CoreProcessOptions {
  repoRoot: string;
  mock: boolean;
  onLog: (line: string, stream: 'out' | 'err') => void;
  onExit: (code: number | null) => void;
}

const MAX_BACKOFF_MS = 15_000;

export class CoreProcess {
  private child: ChildProcess | null = null;
  private stopping = false;
  private restarts = 0;
  private timer: NodeJS.Timeout | null = null;

  constructor(private readonly options: CoreProcessOptions) {}

  /** Prefiere el intérprete del venv del proyecto; si no existe, el del sistema. */
  private pythonExecutable(): string {
    const override = process.env.NOVA_PYTHON;
    if (override) return override;
    const candidates =
      process.platform === 'win32'
        ? [join(this.options.repoRoot, 'core', '.venv', 'Scripts', 'python.exe')]
        : [join(this.options.repoRoot, 'core', '.venv', 'bin', 'python')];
    for (const candidate of candidates) if (existsSync(candidate)) return candidate;
    return process.platform === 'win32' ? 'python' : 'python3';
  }

  start(): void {
    if (this.child || this.stopping) return;
    const executable = this.pythonExecutable();
    const args = ['-u', '-m', 'nova.main'];
    if (this.options.mock) args.push('--mock');

    this.options.onLog(`núcleo: ${executable} ${args.join(' ')}`, 'out');
    this.child = spawn(executable, args, {
      cwd: join(this.options.repoRoot, 'core'),
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
      windowsHide: true,
    });

    this.child.stdout?.on('data', (buffer: Buffer) => this.emitLines(buffer, 'out'));
    this.child.stderr?.on('data', (buffer: Buffer) => this.emitLines(buffer, 'err'));
    this.child.on('exit', (code) => {
      this.child = null;
      this.options.onExit(code);
      if (!this.stopping) this.scheduleRestart();
    });
    this.child.on('error', (error) => this.options.onLog(`no pude lanzar Python: ${error.message}`, 'err'));
  }

  private emitLines(buffer: Buffer, stream: 'out' | 'err'): void {
    for (const line of buffer.toString('utf8').split(/\r?\n/)) {
      if (line.trim()) this.options.onLog(line, stream);
    }
  }

  private scheduleRestart(): void {
    this.restarts += 1;
    const delay = Math.min(MAX_BACKOFF_MS, 700 * 2 ** Math.min(this.restarts, 5));
    this.options.onLog(`el núcleo se detuvo; reintento en ${Math.round(delay / 1000)} s`, 'err');
    this.timer = setTimeout(() => this.start(), delay);
  }

  noteHealthy(): void {
    this.restarts = 0;
  }

  stop(): void {
    this.stopping = true;
    if (this.timer) clearTimeout(this.timer);
    this.child?.kill();
    this.child = null;
  }
}
