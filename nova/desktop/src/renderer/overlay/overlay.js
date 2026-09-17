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
  portrait: () => new window.PortraitAvatar(canvas, 'assets/nova-avatar.webp', 'assets/nova-avatar-sonriendo.webp'),
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
  holdingSubtitle = false;
  if (transcriptTimer) clearTimeout(transcriptTimer);
  if (text && isFinal) {
    transcriptTimer = setTimeout(() => {
      transcriptEl.dataset.visible = 'false';
    }, 6000);
  }
}

// El subtítulo de lo que dice Nova no tiene temporizador: se queda mientras
// habla (una respuesta larga son 20-30 s) y se va SUBTITLE_GRACE_MS después de
// que termine, para acabar de leerlo. Con el temporizador fijo de 6 s se
// esfumaba a media frase.
const SUBTITLE_GRACE_MS = 2500;
let holdingSubtitle = false;

function showSubtitle(text) {
  transcriptEl.textContent = text;
  transcriptEl.dataset.visible = text ? 'true' : 'false';
  transcriptEl.dataset.final = 'true';
  if (transcriptTimer) clearTimeout(transcriptTimer);
  holdingSubtitle = Boolean(text);
}

function releaseSubtitle() {
  if (!holdingSubtitle) return;
  holdingSubtitle = false;
  if (transcriptTimer) clearTimeout(transcriptTimer);
  transcriptTimer = setTimeout(() => {
    transcriptEl.dataset.visible = 'false';
  }, SUBTITLE_GRACE_MS);
}

function askConfirmation(id, text) {
  pendingConfirmId = id;
  confirmText.textContent = text;
  confirmEl.dataset.visible = 'true';
}

function closeConfirmation() {
  pendingConfirmId = null;
  confirmEl.dataset.visible = 'false';
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') window.nova.send({ type: 'stop' });
});

document.getElementById('avatar-host').addEventListener('dblclick', () => window.nova.openSettings());

window.nova.onEvent((event) => {
  switch (event.type) {
    case 'state':
      avatar.setState(event.state);
      showStatus(STATE_LABEL[event.state] ?? '');
      // Terminó de hablar (o ya no va a hablar): el subtítulo se va con gracia.
      if (event.state === 'LISTENING' || event.state === 'IDLE' || event.state === 'SLEEPING') releaseSubtitle();
      if ((event.state === 'IDLE' || event.state === 'SLEEPING') && !holdingSubtitle && !transcriptTimer) showTranscript('', true);
      break;
    case 'status':
      if (event.text) showStatus(event.text);
      break;
    case 'transcript':
      showTranscript(event.text, !event.partial);
      break;
    case 'assistant_text':
      showSubtitle(event.text);
      break;
    case 'level':
      avatar.setLevel(event.value);
      break;
    case 'confirm_request':
      askConfirmation(event.id, event.text);
      break;
    case 'confirm_resolved':
      if (pendingConfirmId === event.id) closeConfirmation();
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
