/**
 * Avatar procedural: una apertura óptica.
 *
 * Forma: un núcleo brillante rodeado de cinco segmentos de arco con huecos, como
 * el diafragma de un objetivo. Se eligió por tres razones prácticas:
 *   - los ocho estados se distinguen a un metro de distancia por color, velocidad
 *     de giro y apertura de los segmentos, sin leer ningún texto;
 *   - cuesta ~0,3 ms por fotograma en canvas 2D, sin cargar WebGL ni assets;
 *   - no depende de ningún archivo externo, así que el MVP arranca sin diseñar nada.
 *
 * Todo lo específico del dibujo está aquí dentro. Para sustituirlo por Rive o
 * Live2D basta con escribir otra clase con los mismos cuatro métodos: setState,
 * setLevel, start y stop. Nada más del sistema toca el canvas.
 */

const PALETTE = {
  IDLE:               { halo: '#4FA8FF', spin: 0.10, gap: 0.30, ring: 1.6 },
  WAKE_WORD_DETECTED: { halo: '#3FE0B0', spin: 1.90, gap: 0.05, ring: 3.2 },
  LISTENING:          { halo: '#3FE0B0', spin: 0.34, gap: 0.14, ring: 2.6 },
  THINKING:           { halo: '#8C7CFF', spin: 1.15, gap: 0.42, ring: 1.8 },
  EXECUTING:          { halo: '#FFAE3D', spin: 0.62, gap: 0.22, ring: 2.4 },
  SPEAKING:           { halo: '#6FBBFF', spin: 0.20, gap: 0.10, ring: 3.0 },
  ERROR:              { halo: '#FF5E6C', spin: 0.05, gap: 0.50, ring: 2.2 },
  SLEEPING:           { halo: '#2A4A6B', spin: 0.05, gap: 0.36, ring: 1.2 },
};

const SEGMENTS = 5;
const TAU = Math.PI * 2;

class ApertureAvatar {
  constructor(canvas) {
    this.canvas = canvas;
    this.context = canvas.getContext('2d');
    this.state = 'IDLE';
    this.level = 0;
    this.smoothLevel = 0;
    this.angle = 0;
    this.blend = { ...PALETTE.IDLE };
    this.frame = null;
    this.lastTime = 0;
    this.reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.resize();
    window.addEventListener('resize', () => this.resize());
  }

  resize() {
    const ratio = window.devicePixelRatio || 1;
    const size = this.canvas.clientWidth;
    this.canvas.width = size * ratio;
    this.canvas.height = size * ratio;
    this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.size = size;
  }

  setState(state) {
    if (PALETTE[state]) this.state = state;
  }

  setLevel(value) {
    this.level = Math.max(0, Math.min(1, value));
  }

  start() {
    if (this.frame !== null) return;
    this.lastTime = performance.now();
    const loop = (time) => {
      const delta = Math.min(0.05, (time - this.lastTime) / 1000);
      this.lastTime = time;
      this.update(delta);
      this.draw(time / 1000);
      this.frame = requestAnimationFrame(loop);
    };
    this.frame = requestAnimationFrame(loop);
  }

  stop() {
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
  }

  /** Interpola hacia el objetivo: los cambios de estado se ven, no saltan. */
  update(delta) {
    const target = PALETTE[this.state] ?? PALETTE.IDLE;
    const k = this.reducedMotion ? 1 : Math.min(1, delta * 6);
    this.blend.spin += (target.spin - this.blend.spin) * k;
    this.blend.gap += (target.gap - this.blend.gap) * k;
    this.blend.ring += (target.ring - this.blend.ring) * k;
    this.blend.halo = target.halo;
    this.smoothLevel += (this.level - this.smoothLevel) * Math.min(1, delta * 12);
    this.level *= 0.94;
    if (!this.reducedMotion) this.angle = (this.angle + this.blend.spin * delta * TAU) % TAU;
  }

  draw(seconds) {
    const ctx = this.context;
    const size = this.size;
    const cx = size / 2;
    const cy = size / 2;
    const unit = size / 200;
    ctx.clearRect(0, 0, size, size);

    const breathing = this.reducedMotion ? 0 : Math.sin(seconds * 1.4) * 1.4 * unit;
    const amplitude = this.smoothLevel;
    const halo = this.blend.halo;

    // Resplandor exterior
    const glow = ctx.createRadialGradient(cx, cy, 6 * unit, cx, cy, 78 * unit);
    glow.addColorStop(0, hexToRgba(halo, 0.30 + amplitude * 0.25));
    glow.addColorStop(0.55, hexToRgba(halo, 0.08));
    glow.addColorStop(1, hexToRgba(halo, 0));
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(cx, cy, 78 * unit, 0, TAU);
    ctx.fill();

    // Segmentos de la apertura
    const radius = (44 + breathing / unit + amplitude * 5) * unit;
    const arc = TAU / SEGMENTS - this.blend.gap;
    ctx.lineCap = 'round';
    ctx.strokeStyle = halo;
    ctx.lineWidth = this.blend.ring * unit;
    ctx.shadowColor = hexToRgba(halo, 0.8);
    ctx.shadowBlur = 12 * unit;
    for (let i = 0; i < SEGMENTS; i += 1) {
      const start = this.angle + (i * TAU) / SEGMENTS;
      const jitter = this.state === 'ERROR' && !this.reducedMotion
        ? Math.sin(seconds * 22 + i) * 0.035
        : 0;
      ctx.globalAlpha = 0.55 + 0.45 * Math.abs(Math.cos(start * 1.5));
      ctx.beginPath();
      ctx.arc(cx, cy, radius, start + jitter, start + arc + jitter);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.shadowBlur = 0;

    // Anillo de amplitud: sólo cuando hay señal que mostrar
    if (amplitude > 0.02) {
      ctx.strokeStyle = hexToRgba(halo, 0.28 + amplitude * 0.4);
      ctx.lineWidth = 1.2 * unit;
      ctx.beginPath();
      ctx.arc(cx, cy, radius + (9 + amplitude * 16) * unit, 0, TAU);
      ctx.stroke();
    }

    // Núcleo
    const coreRadius = (13 + amplitude * 9 + breathing * 0.25) * unit;
    const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreRadius);
    core.addColorStop(0, '#FFFFFF');
    core.addColorStop(0.45, '#F4F9FF');
    core.addColorStop(1, hexToRgba(halo, 0.55));
    ctx.fillStyle = core;
    ctx.shadowColor = hexToRgba(halo, 0.9);
    ctx.shadowBlur = 20 * unit;
    ctx.beginPath();
    ctx.arc(cx, cy, coreRadius, 0, TAU);
    ctx.fill();
    ctx.shadowBlur = 0;
  }
}

function hexToRgba(hex, alpha) {
  const value = parseInt(hex.slice(1), 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
}

window.ApertureAvatar = ApertureAvatar;
