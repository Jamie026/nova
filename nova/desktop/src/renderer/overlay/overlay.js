/**
 * Lógica del overlay: traduce eventos del núcleo en estado visual.
 *
 * Deliberadamente tonto. No decide nada; sólo refleja. Toda la máquina de
 * estados vive en Python, así que la interfaz nunca puede desincronizarse con
 * lo que el agente cree estar haciendo.
 *
 * Script clásico y no módulo ES: Chromium bloquea los módulos servidos por
 * file://, que es como Electron carga los renderers con loadFile.
 */
const canvas = document.getElementById('avatar');

/**
 * Skins intercambiables. Ambos cumplen el mismo contrato (setState, setLevel,
 * start, stop), así que cambiar de uno a otro es construir otra clase: ni el
 * resto de este archivo ni el núcleo se enteran.
 */
const SKINS = {
  portrait: () => new window.PortraitAvatar(canvas, 'assets/nova-avatar.webp'),
  aperture: () => new window.ApertureAvatar(canvas),
};

let skinName = 'portrait';
let avatar = SKINS[skinName]();
avatar.start();

function applySkin(name) {
  const next = SKINS[name] ? name : 'portrait';
  if (next === skinName) return;
  const state = avatar.state;
  avatar.stop();
  skinName = next;
  avatar = SKINS[next]();
  avatar.setState(state);
  avatar.start();
}

const statusEl = document.getElementById('status');
const statusText = document.getElementById('status-text');
const transcriptEl = document.getElementById('transcript');
const confirmEl = document.getElementById('confirm');
const confirmText = document.getElementById('confirm-text');

let pendingConfirmId = null;
let transcriptTimer = null;

const STATE_LABEL = {
  WAKE_WORD_DETECTED: 'Aquí estoy',
  LISTENING: 'Escuchando',
  THINKING: 'Pensando',
  EXECUTING: 'Trabajando',
  SPEAKING: 'Hablando',
  ERROR: 'Algo falló',
  SLEEPING: '',
  IDLE: '',
};

function showStatus(text) {
  statusText.textContent = text;
  statusEl.dataset.visible = text ? 'true' : 'false';
}

function showTranscript(text, isFinal) {
  transcriptEl.textContent = text;
  transcriptEl.dataset.visible = text ? 'true' : 'false';
  transcriptEl.dataset.final = isFinal ? 'true' : 'false';
  if (transcriptTimer) clearTimeout(transcriptTimer);
  if (text && isFinal) {
    transcriptTimer = setTimeout(() => {
      transcriptEl.dataset.visible = 'false';
    }, 6000);
  }
}

function askConfirmation(id, text) {
  pendingConfirmId = id;
  confirmText.textContent = text;
  confirmEl.dataset.visible = 'true';
  document.getElementById('confirm-yes').focus();
}

function answerConfirmation(approved) {
  if (!pendingConfirmId) return;
  window.nova.send({ type: 'confirm_response', id: pendingConfirmId, approved });
  pendingConfirmId = null;
  confirmEl.dataset.visible = 'false';
}

document.getElementById('confirm-yes').addEventListener('click', () => answerConfirmation(true));
document.getElementById('confirm-no').addEventListener('click', () => answerConfirmation(false));

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    if (pendingConfirmId) answerConfirmation(false);
    else window.nova.send({ type: 'stop' });
  }
});

document.getElementById('avatar-host').addEventListener('dblclick', () => window.nova.openSettings());

window.nova.onEvent((event) => {
  switch (event.type) {
    case 'state':
      avatar.setState(event.state);
      showStatus(STATE_LABEL[event.state] ?? '');
      if (event.state === 'IDLE' || event.state === 'SLEEPING') showTranscript('', true);
      break;
    case 'status':
      if (event.text) showStatus(event.text);
      break;
    case 'transcript':
      showTranscript(event.text, !event.partial);
      break;
    case 'assistant_text':
      showTranscript(event.text, true);
      break;
    case 'level':
      avatar.setLevel(event.value);
      break;
    case 'confirm_request':
      askConfirmation(event.id, event.text);
      break;
    case 'confirm_resolved':
      // Contestada por voz o expirada: fuera el panel, sin reenviar nada.
      if (pendingConfirmId === event.id) {
        pendingConfirmId = null;
        confirmEl.dataset.visible = 'false';
      }
      break;
    case 'settings':
      applySkin(event.data?.config?.avatar?.skin ?? 'portrait');
      break;
    case 'error':
      showStatus(event.text || 'Algo falló');
      break;
    default:
      break;
  }
});

window.nova.onConnection(({ connected }) => {
  if (!connected) {
    avatar.setState('SLEEPING');
    showStatus('Conectando con el núcleo…');
  }
});
