/**
 * Avatar de retrato: una imagen fija que no cambia de expresión.
 *
 * El personaje es un PNG/WEBP recortado que no se anima ni se deforma. Todo el
 * lenguaje de estado (escuchando, pensando, ejecutando…) se cuenta *alrededor*
 * del retrato, nunca sobre él:
 *   - un halo de color detrás de la figura, que es el mismo código de color que
 *     usaba la apertura procedural, así que los ocho estados se siguen leyendo
 *     de un vistazo;
 *   - un anillo bajo los pies que respira con el nivel del micrófono;
 *   - una flotación vertical de 3 px, suficiente para que no parezca una captura
 *     pegada al escritorio.
 *
 * Expone exactamente los mismos cuatro métodos que ApertureAvatar (setState,
 * setLevel, start, stop), de modo que overlay.js puede intercambiar un skin por
 * otro sin enterarse de qué está dibujando.
 *
 * Se dibuja en canvas 2D en vez de con CSS porque el halo tiene que mezclarse
 * con la transparencia de la ventana sin recortar la silueta del personaje:
 * con filtros CSS sobre un <img> los bordes del PNG generan un halo cuadrado.
 */

const PORTRAIT_PALETTE = {
  IDLE:               { halo: '#4FA8FF', glow: 0.26, pulse: 0.55, ring: 0.30 },
  WAKE_WORD_DETECTED: { halo: '#3FE0B0', glow: 0.95, pulse: 2.60, ring: 0.95 },
  LISTENING:          { halo: '#3FE0B0', glow: 0.62, pulse: 0.90, ring: 0.80 },
  THINKING:           { halo: '#8C7CFF', glow: 0.58, pulse: 1.70, ring: 0.45 },
  EXECUTING:          { halo: '#FFAE3D', glow: 0.70, pulse: 1.20, ring: 0.60 },
  SPEAKING:           { halo: '#6FBBFF', glow: 0.72, pulse: 0.70, ring: 0.85 },
  ERROR:              { halo: '#FF5E6C', glow: 0.80, pulse: 0.30, ring: 0.40 },
  SLEEPING:           { halo: '#2A4A6B', glow: 0.12, pulse: 0.25, ring: 0.15 },
};

const TAU_P = Math.PI * 2;

function hexToRgb(hex) {
  const value = parseInt(hex.slice(1), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

class PortraitAvatar {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {string} source ruta de la imagen del personaje
   */
  constructor(canvas, source) {
    this.canvas = canvas;
    this.context = canvas.getContext('2d');
    this.state = 'IDLE';
    this.level = 0;
    this.smoothLevel = 0;
    this.phase = 0;
    this.blend = { ...PORTRAIT_PALETTE.IDLE, rgb: hexToRgb(PORTRAIT_PALETTE.IDLE.halo) };
    this.target = { ...this.blend };
    this.frame = null;
    this.lastTime = 0;
    this.ready = false;

    this.image = new Image();
    this.image.onload = () => { this.ready = true; };
    this.image.src = source || 'assets/nova-avatar.webp';

    this.resize = this.resize.bind(this);
    this.tick = this.tick.bind(this);
    window.addEventListener('resize', this.resize);
    this.resize();
  }

  resize() {
    const ratio = window.devicePixelRatio || 1;
    const box = this.canvas.getBoundingClientRect();
    this.width = box.width || 200;
    this.height = box.height || 260;
    this.canvas.width = Math.round(this.width * ratio);
    this.canvas.height = Math.round(this.height * ratio);
    this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  setState(state) {
    if (!PORTRAIT_PALETTE[state]) return;
    this.state = state;
    this.target = { ...PORTRAIT_PALETTE[state], rgb: hexToRgb(PORTRAIT_PALETTE[state].halo) };
  }

  /** @param {number} value 0..1, energía del micrófono */
  setLevel(value) {
    this.level = Math.max(0, Math.min(1, value));
  }

  start() {
    if (this.frame === null) {
      this.lastTime = performance.now();
      this.frame = requestAnimationFrame(this.tick);
    }
  }

  stop() {
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
    window.removeEventListener('resize', this.resize);
  }

  tick(now) {
    const delta = Math.min((now - this.lastTime) / 1000, 0.05);
    this.lastTime = now;

    // Interpolación exponencial: los cambios de estado se sienten como una
    // transición y no como un corte, sin necesidad de temporizadores.
    const k = 1 - Math.exp(-delta * 7);
    this.blend.glow += (this.target.glow - this.blend.glow) * k;
    this.blend.pulse += (this.target.pulse - this.blend.pulse) * k;
    this.blend.ring += (this.target.ring - this.blend.ring) * k;
    for (let i = 0; i < 3; i += 1) {
      this.blend.rgb[i] += (this.target.rgb[i] - this.blend.rgb[i]) * k;
    }
    this.smoothLevel += (this.level - this.smoothLevel) * (1 - Math.exp(-delta * 12));
    this.phase = (this.phase + delta * this.blend.pulse) % 1000;

    this.draw();
    this.frame = requestAnimationFrame(this.tick);
  }

  draw() {
    const ctx = this.context;
    const w = this.width;
    const h = this.height;
    ctx.clearRect(0, 0, w, h);

    const [r, g, b] = this.blend.rgb.map((v) => Math.round(v));
    const breath = 0.5 + 0.5 * Math.sin(this.phase * TAU_P);
    const energy = this.blend.glow * (0.72 + 0.28 * breath) + this.smoothLevel * 0.35;

    // 1. Halo detrás del personaje.
    const cx = w / 2;
    const cy = h * 0.46;
    const radius = Math.min(w, h) * (0.46 + 0.05 * breath + this.smoothLevel * 0.06);
    const halo = ctx.createRadialGradient(cx, cy, radius * 0.15, cx, cy, radius);
    halo.addColorStop(0, `rgba(${r}, ${g}, ${b}, ${0.34 * energy})`);
    halo.addColorStop(0.55, `rgba(${r}, ${g}, ${b}, ${0.14 * energy})`);
    halo.addColorStop(1, 'rgba(0, 0, 0, 0)');
    ctx.fillStyle = halo;
    ctx.fillRect(0, 0, w, h);

    // 2. Anillo de suelo: da peso a la figura y marca el nivel de micrófono.
    const ringY = h * 0.95;
    const ringW = w * (0.30 + this.blend.ring * 0.14 + this.smoothLevel * 0.12);
    const ringH = ringW * 0.20;
    ctx.save();
    ctx.translate(cx, ringY);
    ctx.scale(1, ringH / ringW);
    const ring = ctx.createRadialGradient(0, 0, ringW * 0.15, 0, 0, ringW);
    ring.addColorStop(0, `rgba(${r}, ${g}, ${b}, ${0.55 * this.blend.ring})`);
    ring.addColorStop(1, 'rgba(0, 0, 0, 0)');
    ctx.fillStyle = ring;
    ctx.beginPath();
    ctx.arc(0, 0, ringW, 0, TAU_P);
    ctx.fill();
    ctx.restore();

    if (!this.ready) return;

    // 3. El retrato, escalado para caber entero y con una flotación mínima.
    const float = Math.sin(this.phase * TAU_P * 0.5) * 3;
    const scale = Math.min(w / this.image.width, (h * 0.92) / this.image.height);
    const dw = this.image.width * scale;
    const dh = this.image.height * scale;
    const dx = cx - dw / 2;
    const dy = h * 0.96 - dh + float;

    // La sombra de canvas sigue el canal alfa del PNG, así que el resplandor
    // abraza la silueta del personaje en lugar de dibujar un rectángulo.
    ctx.save();
    ctx.shadowColor = `rgba(${r}, ${g}, ${b}, ${0.55 * energy})`;
    ctx.shadowBlur = 18 + 26 * energy;
    ctx.drawImage(this.image, dx, dy, dw, dh);
    ctx.restore();
  }
}

window.PortraitAvatar = PortraitAvatar;
