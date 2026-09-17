#!/usr/bin/env node
/**
 * Lanza el intérprete de Python del núcleo con el directorio de trabajo correcto.
 *
 * Los scripts de npm llamaban a `python` a secas desde la raíz del repo, y eso
 * está mal por dos motivos a la vez: usa el Python del sistema en vez del venv
 * de core/ (donde están faster-whisper, CUDA y todo lo demás) y arranca fuera de
 * core/, así que ni el paquete `nova` ni core/pytest.ini existen para él. El
 * resultado era que `npm run test:core` fallaba al recoger las pruebas y
 * `npm run core` ni siquiera encontraba el módulo.
 *
 * Es la misma elección de intérprete que hace desktop/src/main/core-process.ts
 * cuando Electron arranca el núcleo; aquí sólo se replica para la línea de
 * comandos.
 */
const { spawnSync } = require('node:child_process');
const { existsSync } = require('node:fs');
const { join } = require('node:path');

const root = join(__dirname, '..');
const core = join(root, 'core');
const isWindows = process.platform === 'win32';

const venv = isWindows
  ? join(core, '.venv', 'Scripts', 'python.exe')
  : join(core, '.venv', 'bin', 'python');
const python = existsSync(venv) ? venv : isWindows ? 'python' : 'python3';

if (!existsSync(venv)) {
  console.warn(
    `[nova] No hay venv en ${venv}; uso "${python}" del sistema.\n` +
      '       Si falla algo, ejecuta primero: npm run setup:win  (o scripts/setup.sh)'
  );
}

const result = spawnSync(python, process.argv.slice(2), { cwd: core, stdio: 'inherit' });
if (result.error) {
  console.error(`[nova] No pude ejecutar ${python}: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
