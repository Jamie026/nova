"""Voz clonada con Chatterbox Turbo (Resemble AI, MIT).

Por qué éste y no XTTS, medido con las mismas 12 frases × 3 tomas, la misma
referencia de Corin y los mismos jueces (Whisper medium para entender, un
codificador de hablante independiente para el parecido contra las líneas
reales de Corin):

                    se entiende   malas/36   desbocadas   parecido   rtf (RTX 3050)
    Chatterbox Turbo    0,991         0           0         0,61        0,55
    XTTS-v2             0,88        5-7          1          0,64        0,44

XTTS se parece algo más a la referencia (0,64 frente a 0,61; el techo es
0,68) pero produce basura en un 5-10 % de las tomas («the… the… the…»,
«Kiss. Kiss.», susurros) y a veces se desboca; había que vigilarlo con
Whisper y repetir tomas. Chatterbox Turbo no falló ni una vez en 36, ni con
frases de una palabra ni con horas («It's 10:44 PM.»), y va a la misma
velocidad. El Chatterbox normal (con CFG y 10 pasos de decodificador) es
igual de fiable pero 2,5x más lento y no cabe en tiempo real aquí.

Cómo se usa el modelo:
  * La referencia se prepara UNA vez (prepare_conditionals cuesta 5 s). El
    banco que pasaba la referencia en cada llamada medía rtf 2,4-4,8 por eso.
  * Sólo usa los primeros 10 s de la referencia (6 s para el timbre), así que
    `tts.reference` debe empezar por sus mejores segundos.
  * norm_loudness=False: la 0.1.7 convierte la referencia a float64 al
    normalizarla y el tokenizador de audio no lo acepta; nuestra referencia ya
    viene normalizada de voice_profile.
  * El bucle de tokens (T3) está copiado de inference_turbo con dos añadidos:
    se puede cancelar (barge-in) y tiene tope proporcional al texto. El
    decodificador (S3Gen) no es causal, así que no se decodifica por prefijos:
    cada oración es una toma entera. Con rtf 0,55 una respuesta de 3,5 s
    empieza a sonar a los ~2 s, y las oraciones siguientes se generan
    mientras suena la anterior.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

from .text import plan_pieces

log = logging.getLogger("nova.tts")

OUTPUT_RATE = 24_000
_TOKENS_PER_SECOND = 25.0      # tokens de habla de S3 por segundo de audio
_CHARS_PER_SECOND = 19.0       # medido con esta voz: 95 tokens (3,8 s) para 80 letras
_RTF_GUESS = 0.6               # lo que tarda generar un segundo de audio, hasta medirlo
_S3GEN_FIXED_S = 0.3           # coste casi fijo del decodificador por toma
_VERIFY_BELOW_CHARS = 40       # las cortas se comprueban con Whisper antes de sonar
_VERIFY_ATTEMPTS = 3
_VERIFY_MIN_MATCH = 0.75


class _Player:
    """Un único OutputStream por respuesta (abrirlo cuesta 150-400 ms)."""

    def __init__(self, device, cancel: asyncio.Event):
        self._device = device
        self._cancel = cancel
        self._stream: Optional[sd.OutputStream] = None

    def _ensure(self) -> None:
        if self._stream is None:
            self._stream = sd.OutputStream(samplerate=OUTPUT_RATE, channels=1,
                                           dtype="float32", device=self._device)
            self._stream.start()

    async def prepare(self) -> None:
        try:
            await asyncio.to_thread(self._ensure)
        except Exception as exc:
            log.debug("no se pudo preabrir la salida de audio: %s", exc)

    async def play(self, samples: np.ndarray, on_level=None) -> bool:
        loop = asyncio.get_running_loop()
        data = samples.reshape(-1, 1)

        def _write() -> bool:
            self._ensure()
            assert self._stream is not None
            for start in range(0, len(data), 1024):
                if self._cancel.is_set():
                    return False
                piece = data[start:start + 1024]
                self._stream.write(piece)
                if on_level is not None:
                    loop.call_soon_threadsafe(on_level, float(min(1.0, np.abs(piece).mean() * 6.0)))
            return True

        return await asyncio.to_thread(_write)

    def _close_stream(self) -> None:
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        try:
            if self._cancel.is_set():
                stream.abort()
            else:
                stream.stop()
            stream.close()
        except Exception:
            pass

    async def close(self) -> None:
        await asyncio.to_thread(self._close_stream)


class ChatterboxEngine:
    def __init__(self, reference: str | Path, device: str = "auto",
                 output_device: Optional[int | str] = None, language: str = "en"):
        self._reference = Path(reference)
        self._device_pref = device
        self._output_device = output_device
        self._language = language          # sólo inglés; se acepta el parámetro por el Protocol
        self._model = None
        self._torch = None
        self._cancel = asyncio.Event()
        self._lock = asyncio.Lock()        # una generación en la GPU a la vez
        self._loading: Optional[asyncio.Task] = None
        self._rtf = _RTF_GUESS              # se ajusta con cada toma
        # La sesión lo pone: `async (pcm16_bytes, idioma) -> texto oído`.
        self.verifier = None

    # ------------------------------------------------------------------ carga
    def _resolve_device(self) -> str:
        if self._device_pref != "auto":
            return self._device_pref
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def _load(self):
        import os

        # El decodificador pinta una barra de tqdm por cada toma en stderr, que
        # la interfaz muestra como líneas de log. Fuera.
        os.environ.setdefault("TQDM_DISABLE", "1")
        import torch
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        from ..stt.faster_whisper_stt import MODEL_LOAD_LOCK

        device = self._resolve_device()
        # Bajo el mismo cerrojo que los modelos de Whisper: dos motores
        # inicializando CUDA a la vez en hilos distintos mataban el proceso.
        with MODEL_LOAD_LOCK:
            model = ChatterboxTurboTTS.from_pretrained(device=device)
            model.prepare_conditionals(str(self._reference), norm_loudness=False)
        self._torch = torch
        return model

    @property
    def ready(self) -> bool:
        """True cuando la voz puede sonar sin esperar a que cargue nada."""
        return self._model is not None

    async def warmup(self) -> None:
        if self._model is not None:
            return
        if self._loading is None:
            self._loading = asyncio.create_task(self._warmup_once(), name="tts-load")
        try:
            await asyncio.shield(self._loading)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._loading = None
            raise

    async def _warmup_once(self) -> None:
        log.info("cargando Chatterbox Turbo (voz de %s)", self._reference.name)
        model = await asyncio.to_thread(self._load)
        async with self._lock:
            self._model = model
            # Los núcleos de CUDA se compilan en la primera síntesis; que no la
            # pague la primera respuesta real.
            await asyncio.to_thread(self._synthesize, "Hi! I'm ready. Just tell me what you need and I'll take care of it.")
            await asyncio.to_thread(self._synthesize, "Okay.")
        log.info("Chatterbox Turbo listo en %s", self._resolve_device())

    # --------------------------------------------------------------- síntesis
    def _synthesize(self, text: str, stats: dict | None = None) -> np.ndarray:
        """Una toma entera. Copia de ChatterboxTurboTTS.generate + inference_turbo,
        con cancelación y tope de tokens."""
        torch = self._torch
        m = self._model
        stats = stats if stats is not None else {}
        from chatterbox.tts import punc_norm
        from chatterbox.tts_turbo import S3GEN_SIL
        from transformers.generation.logits_process import (
            LogitsProcessorList, RepetitionPenaltyLogitsProcessor, TemperatureLogitsWarper,
            TopKLogitsWarper, TopPLogitsWarper)

        text = punc_norm(text)
        expected_tokens = len(text) / _CHARS_PER_SECOND * _TOKENS_PER_SECOND
        max_tokens = int(expected_tokens * 1.8) + 50
        t3 = m.t3
        started = time.perf_counter()
        with torch.inference_mode():
            text_tokens = m.tokenizer(text, return_tensors="pt", padding=True, truncation=True).input_ids.to(m.device)
            processors = LogitsProcessorList([TemperatureLogitsWarper(0.8), TopKLogitsWarper(1000),
                                              TopPLogitsWarper(0.95), RepetitionPenaltyLogitsProcessor(1.2)])
            start = t3.hp.start_speech_token * torch.ones_like(text_tokens[:, :1])
            embeds, _ = t3.prepare_input_embeds(t3_cond=m.conds.t3, text_tokens=text_tokens,
                                                speech_tokens=start, cfg_weight=0.0)
            out = t3.tfmr(inputs_embeds=embeds, use_cache=True)
            past = out.past_key_values
            logits = t3.speech_head(out[0][:, -1:])
            probs = torch.softmax(processors(start, logits[:, -1, :]), dim=-1)
            token = torch.multinomial(probs, num_samples=1)
            generated = [token]
            for _ in range(max_tokens):
                if self._cancel.is_set():
                    stats["cancelled"] = True
                    return np.zeros(0, dtype=np.float32)
                out = t3.tfmr(inputs_embeds=t3.speech_emb(token), past_key_values=past, use_cache=True)
                past = out.past_key_values
                logits = t3.speech_head(out[0])
                scores = processors(torch.cat(generated, dim=1), logits[:, -1, :])
                if torch.all(scores == -float("inf")):
                    break
                token = torch.multinomial(torch.softmax(scores, dim=-1), num_samples=1)
                if torch.all(token == t3.hp.stop_speech_token):
                    break
                generated.append(token)
            else:
                stats["hit_cap"] = True
            speech = torch.cat(generated, dim=1)[0]
            speech = speech[speech < 6561]
            speech = torch.cat([speech, torch.tensor([S3GEN_SIL] * 3, device=m.device).long()])
            wav, _ = m.s3gen.inference(speech_tokens=speech, ref_dict=m.conds.gen, n_cfm_timesteps=2)
            wav = wav.squeeze(0).detach().cpu().numpy()
            wav = m.watermarker.apply_watermark(wav, sample_rate=m.sr)
        wav = np.asarray(wav, dtype=np.float32).reshape(-1)
        stats["tokens"] = int(speech.numel())
        stats["gen_s"] = time.perf_counter() - started
        if len(wav):
            self._rtf = 0.7 * self._rtf + 0.3 * (stats["gen_s"] / (len(wav) / OUTPUT_RATE))
        return wav

    async def _heard_match(self, text: str, wav: np.ndarray) -> float:
        import difflib
        import re

        if self.verifier is None or wav.size == 0:
            return 1.0
        try:
            pcm = (np.interp(np.linspace(0, len(wav) - 1, int(len(wav) * 16000 / OUTPUT_RATE)),
                             np.arange(len(wav)), wav) * 32767).astype(np.int16).tobytes()
            heard = await self.verifier(pcm, "en")
        except Exception as exc:
            log.debug("no pude comprobar la voz: %s", exc)
            return 1.0
        norm = lambda t: " ".join(re.sub(r"[^\w\s]", " ", t.lower()).split())
        a, b = norm(text), norm(heard)
        if not a:
            return 1.0
        return max(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(),
                   difflib.SequenceMatcher(None, a.replace(" ", ""), b.replace(" ", ""), autojunk=False).ratio())

    async def _take(self, text: str) -> np.ndarray:
        """Una toma entera bajo el cerrojo de la GPU. Las frases cortas
        (menos de _VERIFY_BELOW_CHARS) las aprueba Whisper antes: medido, el
        modelo no falló ninguna de 36, pero las de una palabra son el único
        sitio donde cualquier modelo tropieza y comprobarlas cuesta 0,3 s."""
        verify = self.verifier is not None and len(text.strip()) < _VERIFY_BELOW_CHARS
        best: np.ndarray | None = None
        best_score = -1.0
        for attempt in range(_VERIFY_ATTEMPTS if verify else 1):
            stats: dict = {}
            async with self._lock:
                if self._cancel.is_set():
                    return np.zeros(0, dtype=np.float32)
                wav = await asyncio.to_thread(self._synthesize, text, stats)
            if stats.get("cancelled"):
                return wav
            log.info("voz: «%s…» %d tokens, %.1f s de audio en %.1f s (rtf %.2f)%s", text[:30],
                     stats.get("tokens", 0), len(wav) / OUTPUT_RATE, stats.get("gen_s", 0.0),
                     stats.get("gen_s", 0.0) / max(0.1, len(wav) / OUTPUT_RATE),
                     ", llegó al tope" if stats.get("hit_cap") else "")
            if not verify:
                return wav
            score = await self._heard_match(text, wav)
            if score > best_score:
                best, best_score = wav, score
            if score >= _VERIFY_MIN_MATCH:
                return wav
            log.warning("la voz no dijo «%s» (parecido %.2f); %s", text, score,
                        "reintento" if attempt + 1 < _VERIFY_ATTEMPTS else "me quedo con la mejor toma")
        return best if best is not None else np.zeros(0, dtype=np.float32)

    def _initial_delay(self, texts: list[str], first_seconds: float | None = None) -> float:
        """Cuánto esperar antes de empezar a sonar para que la tubería no se
        quede sin audio entre oraciones. Cada oración i estará lista en
        ready_i = ready_{i-1} + rtf·dur_i + coste fijo, y tiene que sonar en
        play_i = play_1 + Σ dur_j (j<i). El menor play_1 que cumple todas es
        max_i(ready_i − Σ_{j<i} dur_j). Con oraciones parecidas es ~0; el caso
        malo es una primera oración corta seguida de una larga."""
        durs = [max(0.8, len(t) / _CHARS_PER_SECOND) for t in texts]
        if first_seconds:
            durs[0] = first_seconds          # la primera ya existe: su duración real
        ready = 0.0        # instante en que la oración i está generada
        need = 0.0
        acc = 0.0          # audio que ya habrá sonado cuando toque la oración i
        base = None        # el reloj arranca cuando la primera está lista
        for i, dur in enumerate(durs):
            ready += self._rtf * dur + _S3GEN_FIXED_S
            if base is None:
                base = ready
            else:
                need = max(need, (ready - base) - acc)
            acc += dur
        return need

    async def speak(self, text: str, on_level=None, language: str | None = None) -> bool:
        if self._model is None:
            await self.warmup()
        self._cancel.clear()
        # Primera pieza de hasta 50 letras (~2,6 s de audio, ~1,9 s de espera
        # antes de oír nada) y las siguientes creciendo 1,5x: es lo que la
        # tubería absorbe sin esperar (ver plan_pieces).
        texts = plan_pieces(text, max_chars=140, min_chars=30, first_max=50, growth=1.5)
        if not texts:
            return True
        player = _Player(self._output_device, self._cancel)
        takes = [asyncio.create_task(self._take(t), name=f"tts-take-{i}") for i, t in enumerate(texts)]
        completed = False
        try:
            await player.prepare()
            first = await takes[0]
            if self._cancel.is_set() or first.size == 0:
                return False
            delay = self._initial_delay(texts, first_seconds=len(first) / OUTPUT_RATE)
            if delay > 0.05:
                log.debug("voz: espero %.1f s para que las oraciones siguientes lleguen a tiempo", delay)
                await asyncio.sleep(min(delay, 3.0))
            if not await player.play(first, on_level):
                return False
            for take in takes[1:]:
                wav = await take
                if self._cancel.is_set() or wav.size == 0:
                    return False
                if not await player.play(wav, on_level):
                    return False
            completed = True
            return True
        except Exception as exc:
            log.error("fallo de síntesis: %s", exc)
            return False
        finally:
            for take in takes:
                if not take.done():
                    take.cancel()
            if not completed:
                self._cancel.set()
            await player.close()

    async def stop(self) -> None:
        self._cancel.set()

    async def close(self) -> None:
        await self.stop()
        if self._loading is not None and not self._loading.done():
            self._loading.cancel()
        self._model = None
