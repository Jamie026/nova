/**
 * Panel de ajustes y registro en vivo.
 *
 * Los campos se enlazan por data-path contra el árbol de configuración del
 * núcleo, así que añadir un ajuste nuevo es añadir un <input data-path="...">:
 * no hay que tocar este archivo.
 */

const state = { config: {}, loaded: false };

/* ------------------------------------------------------------- utilidades */
const getPath = (object, dotted) =>
  dotted.split('.').reduce((node, key) => (node == null ? undefined : node[key]), object);

const setPath = (object, dotted, value) => {
  const keys = dotted.split('.');
  const last = keys.pop();
  const parent = keys.reduce((node, key) => (node[key] = node[key] ?? {}), object);
  parent[last] = value;
  return object;
};

/* --------------------------------------------------------------- pestañas */
document.getElementById('rail').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-pane]');
  if (!button) return;
  for (const item of document.querySelectorAll('#rail button')) {
    item.setAttribute('aria-current', String(item === button));
  }
  for (const pane of document.querySelectorAll('.pane')) {
    pane.dataset.active = String(pane.dataset.pane === button.dataset.pane);
  }
});

/* ---------------------------------------------------------------- ajustes */
function applyConfig(config) {
  state.config = config;
  state.loaded = true;
  for (const button of document.querySelectorAll('[data-save]')) button.disabled = false;
  for (const field of document.querySelectorAll('[data-path]')) {
    const value = getPath(config, field.dataset.path);
    if (value === undefined) continue;
    if (field.type === 'checkbox') field.checked = Boolean(value);
    else if (field.hasAttribute('data-list')) field.value = (value || []).join(', ');
    else field.value = value;
  }
}

function collectPatch() {
  const patch = {};
  for (const field of document.querySelectorAll('[data-path]')) {
    let value;
    if (field.type === 'checkbox') value = field.checked;
    else if (field.hasAttribute('data-list')) {
      value = field.value.split(',').map((item) => item.trim()).filter(Boolean);
    } else if (field.type === 'number') value = Number(field.value);
    else value = field.value;
    setPath(patch, field.dataset.path, value);
  }
  return patch;
}

/* Guardar antes de recibir la configuración mandaría el formulario vacío, y el
 * núcleo lo aceptaría: cada ajuste quedaría en 0 o en blanco en settings.json. */
for (const button of document.querySelectorAll('[data-save]')) {
  button.disabled = true;
  button.addEventListener('click', () => {
    if (!state.loaded) return;
    window.nova.send({ type: 'set_settings', patch: collectPatch() });
    button.textContent = 'Guardado';
    setTimeout(() => (button.textContent = 'Guardar cambios'), 1600);
  });
}

/* ---------------------------------------------------------------- consola */
const promptInput = document.getElementById('prompt');
const sendPrompt = () => {
  const text = promptInput.value.trim();
  if (!text) return;
  window.nova.send({ type: 'text_input', text });
  promptInput.value = '';
};

document.getElementById('send').addEventListener('click', sendPrompt);
promptInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') sendPrompt();
});
document.getElementById('wake').addEventListener('click', () => window.nova.send({ type: 'wake' }));
document.getElementById('stop').addEventListener('click', () => window.nova.send({ type: 'stop' }));

/* ---------------------------------------------------------------- registro */
const stream = document.getElementById('stream');
let rows = 0;

function append(text, kind = 'info') {
  if (rows === 0) stream.innerHTML = '';
  const row = document.createElement('div');
  row.className = 'row';
  row.dataset.kind = kind;

  const time = document.createElement('time');
  time.textContent = new Date().toLocaleTimeString('es-PE', { hour12: false });
  const body = document.createElement('span');
  body.textContent = text;

  row.append(time, body);
  stream.append(row);
  rows += 1;
  if (rows > 400) {
    stream.firstElementChild?.remove();
    rows -= 1;
  }
  stream.scrollTop = stream.scrollHeight;
}

document.getElementById('clear-log').addEventListener('click', () => {
  stream.innerHTML = '<p class="empty">Registro limpio.</p>';
  rows = 0;
});

/* ------------------------------------------------------------- eventos */
const STATE_TEXT = {
  IDLE: 'En reposo', WAKE_WORD_DETECTED: 'Despierta', LISTENING: 'Escuchando',
  THINKING: 'Pensando', EXECUTING: 'Ejecutando', SPEAKING: 'Hablando',
  ERROR: 'Error', SLEEPING: 'Durmiéndose',
};

const stateEl = document.getElementById('state');
const stateText = document.getElementById('state-text');

window.nova.onEvent((event) => {
  switch (event.type) {
    case 'state':
      stateEl.dataset.state = event.state;
      stateText.textContent = STATE_TEXT[event.state] ?? event.state;
      append(`estado → ${event.state}`, 'info');
      break;
    case 'transcript':
      append(`${event.partial ? 'parcial' : 'final  '} · ${event.text}`, 'voice');
      break;
    case 'tool_call':
      append(`herramienta ${event.name} ${JSON.stringify(event.args)}`, 'tool');
      break;
    case 'tool_result':
      append(`${event.ok ? 'resultado' : 'fallo   '} · ${event.summary}`, event.ok ? 'tool' : 'err');
      break;
    case 'assistant_text':
      append(`nova · ${event.text}`, 'say');
      // Cuando la voz habla otro idioma que los subtítulos, el log deja ver
      // lo que realmente sonó: si no, es imposible saber si lo que oíste
      // mal fue el sintetizador o la traducción.
      if (event.spoken) append(`  voz · ${event.spoken}`, 'info');
      break;
    case 'status':
      if (event.text) append(event.text, 'think');
      break;
    case 'confirm_request':
      append(`confirmación pendiente (${event.risk}) · ${event.text}`, 'err');
      break;
    case 'error':
      append(event.text, 'err');
      break;
    case 'settings':
      if (event.data?.config) applyConfig(event.data.config);
      break;
    default:
      break;
  }
});

window.nova.onLog(({ line, stream: source }) => append(line, source === 'err' ? 'err' : 'info'));

window.nova.onConnection(({ connected }) => {
  if (connected) {
    append('conectado al núcleo', 'info');
    window.nova.send({ type: 'get_settings' });
  } else {
    stateEl.dataset.state = 'IDLE';
    stateText.textContent = 'Sin conexión';
    append('sin conexión con el núcleo; reintentando…', 'err');
  }
});

window.nova.send({ type: 'get_settings' });
