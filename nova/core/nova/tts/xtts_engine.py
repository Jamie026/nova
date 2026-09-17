"""TTS con voz clonada: XTTS-v2 en local, sobre la GPU.

Por qué este modelo y no un conversor de timbre sobre una voz española: con 41 s
de líneas de videojuego no hay material para entrenar nada (RVC pide diez minutos
largos), y sobre todo un conversor cambia el *timbre* pero conserva la entonación
de la voz de origen. Aquí se pidió la voz «tal cual, acento, entonación y todo»,
y eso sólo lo da un modelo que genera la prosodia ya en esa voz. XTTS-v2 clona sin
entrenar, a partir de unos segundos de referencia, y habla español con el acento
del hablante de referencia —que aquí es inglés, y es justo lo que se busca.

Lo que cuesta: entre 0,6 y 1,5 s por frase en una RTX 3050, y ~2 GB de VRAM que
conviven con Whisper. A cambio, se va la dependencia de red de edge-tts.

Contrato idéntico al de EdgeTts (speak / stop / close), así que la sesión no se
entera de cuál de los dos está montado. El troceado y el adelantado de frases son
los mismos: sintetizar el trozo siguiente mientras suena el actual y mantener un
único stream de salida abierto es lo que hace que la respuesta se oiga seguida.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd

from .text import plan_chunks_streaming, plan_whole

log = logging.getLogger("nova.tts")

MODEL_ID = "tts_models/multilingual/multi-dataset/xtts_v2"
OUTPUT_RATE = 24_000

# Por debajo de esto la frase se sintetiza entera y se comprueba antes de sonar
# (cabe reintentar si se atasca: cuesta menos de un segundo). Por encima, se
# emite en streaming: el audio empieza a sonar tras los primeros tokens en vez
# de al final. Es la MISMA pasada del modelo que la síntesis entera —no se
# trocea el texto— así que la voz es idéntica; sólo cambia cuándo empieza.
_STREAM_FROM_CHARS = 40
# Tokens del GPT por trozo de audio: cada token son ~43 ms. 20 tokens = ~0,85 s
# de audio por trozo, que es el primer sonido en menos de medio segundo.
_STREAM_CHUNK_TOKENS = 20

# Un hablante normal suelta 12-18 caracteres por segundo; por debajo de 8 ya no
# está hablando despacio, está atascado. XTTS tiene un modo de fallo muy
# reconocible en el que repite la primera palabra («I, I, I, I, I couldn't
# close…») y el audio sale al doble de largo de lo que toca. Es el peor de sus
# fallos porque no se entiende nada, y es el más fácil de cazar: no hace falta
# escuchar el audio ni transcribirlo, basta con mirar cuánto dura.
# 9 y no 8: con 8, una frase de 131 caracteres estirada a 16 s (el modelo
# metiendo pausas y alargando sílabas) pasaba el filtro. Con 9 se caza; una
# lectura lenta pero normal de esa misma frase (10,5 s) sigue pasando.
_MIN_CHARS_PER_SECOND = 9.0
_DURATION_GRACE_S = 1.2   # margen para el silencio de entrada y de salida


def looks_degenerate(text: str, seconds: float) -> bool:
    """¿Este audio dura tanto que sólo puede ser un atasco del modelo?"""
    limpio = text.strip()
    if not limpio or seconds <= 0:
        return False
    return seconds > len(limpio) / _MIN_CHARS_PER_SECOND + _DURATION_GRACE_S


class _Player:
    """Un único stream de salida reutilizado por todos los trozos de una respuesta.

    Es el mismo de edge_tts_engine: abrir el dispositivo cuesta 150-400 ms en
    Windows y deja un chasquido, así que se abre una vez por respuesta y no una
    vez por frase.
    """

    def __init__(self, device, cancel: asyncio.Event):
        self._device = device
        self._cancel = cancel
        self._stream: Optional[sd.OutputStream] = None

    def _ensure(self) -> None:
        if self._stream is not None:
            return
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
        block = 1024
        data = samples.reshape(-1, 1)

        def _write() -> bool:
            self._ensure()
            assert self._stream is not None
            for start in range(0, len(data), block):
                if self._cancel.is_set():
                    return False
                piece = data[start:start + block]
                self._stream.write(piece)
                if on_level is not None:
                    level = float(min(1.0, np.abs(piece).mean() * 6.0))
                    loop.call_soon_threadsafe(on_level, level)
            return True

        return await asyncio.to_thread(_write)

    def _close_stream(self) -> None:
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        try:
            # abort() si se canceló: stop() drena el búfer, que es justo lo que no
            # queremos tras un barge-in. En el final normal sí interesa drenarlo.
            if self._cancel.is_set():
                stream.abort()
            else:
                stream.stop()
            stream.close()
        except Exception:
            pass

    async def close(self) -> None:
        await asyncio.to_thread(self._close_stream)


class XttsEngine:
    def __init__(self, reference: str | Path, language: str = "es",
                 device: str = "auto", speed: float = 1.0,
                 temperature: float = 0.5,
                 output_device: Optional[int | str] = None,
                 chunking: str = "whole", model_path: str | Path | None = None):
        self._reference = Path(reference)
        self._chunking = chunking
        # Carpeta con un XTTS ajustado a esta voz (model.pth + config.json +
        # vocab.json), la que deja `npm run voice:train`. Vacío = el XTTS-v2
        # de serie con clonado zero-shot.
        self._model_path = Path(model_path) if model_path else None
        self._language = language
        self._speed = float(speed)
        self._temperature = float(temperature)
        self._device_pref = device
        self._output_device = output_device
        self._model = None
        self._latents = None
        self._torch = None
        self._cancel = asyncio.Event()
        self._lock = asyncio.Lock()
        # La carga en curso, para que quien llegue mientras tanto la espere en
        # vez de lanzar otra. Ver warmup().
        self._loading: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ carga
    def _resolve_device(self) -> str:
        if self._device_pref in ("cpu", "cuda"):
            return self._device_pref
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def _load(self):
        # El modelo es CPML (uso no comercial) y la descarga pide aceptarlo. Se
        # marca aquí para que no abra un prompt interactivo en mitad del arranque
        # de un proceso sin terminal; la decisión de usarlo es de quien lo instala.
        os.environ.setdefault("COQUI_TOS_AGREED", "1")
        import torch
        from TTS.api import TTS

        device = self._resolve_device()
        if self._model_path is not None:
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts

            cfg = XttsConfig()
            cfg.load_json(str(self._model_path / "config.json"))
            synth = Xtts.init_from_config(cfg)
            synth.load_checkpoint(cfg, checkpoint_dir=str(self._model_path),
                                  checkpoint_path=str(self._model_path / "model.pth"),
                                  vocab_path=str(self._model_path / "vocab.json"),
                                  use_deepspeed=False)
            synth.to(device)
            synth.eval()
            log.info("XTTS ajustado cargado de %s", self._model_path)
        else:
            model = TTS(MODEL_ID).to(device)
            synth = model.synthesizer.tts_model

        # Los «latents» son la referencia ya digerida: condicionan cada frase con
        # el timbre y el acento. Calcularlos una vez al arrancar y no en cada
        # respuesta ahorra ~400 ms por frase, que es casi la mitad del coste.
        gpt_latent, speaker = synth.get_conditioning_latents(
            audio_path=[str(self._reference)]
        )
        self._latents = (gpt_latent, speaker)
        self._torch = torch
        return synth

    async def warmup(self) -> None:
        """Carga el modelo una sola vez, aunque lo pidan varios a la vez.

        La versión anterior comprobaba `self._model is None` y se ponía a cargar.
        Como el arranque carga en segundo plano y tarda un minuto, la primera vez
        que el usuario hablaba `speak()` veía el modelo todavía a None y lanzaba
        una SEGUNDA carga en paralelo: dos copias de 1,8 GB en una tarjeta de
        6 GB, junto con Whisper. El resultado en el log fue el proceso GPU de
        Electron muriendo dos veces (avatar en blanco), Whisper tardando 7 s en
        vez de 0,3 por la GPU saturada, y una respuesta de cuatro palabras que
        tardó 21 s en sonar. Ahora la carga es una tarea compartida: el segundo
        que llega la espera.
        """
        if self._model is not None:
            return
        if self._loading is None or self._loading.done():
            self._loading = asyncio.create_task(self._warmup_once(), name="xtts-load")
        await asyncio.shield(self._loading)

    async def _warmup_once(self) -> None:
        log.info("cargando XTTS-v2 (voz de %s)", self._reference.name)
        synth = await asyncio.to_thread(self._load)
        # La frase de calentamiento va bajo el mismo cerrojo que las de verdad:
        # dos inferencias del mismo modelo desde dos hilos a la vez sobre la GPU
        # no van más rápido, y con CUDA es la forma de acabar con la memoria
        # corrupta o con un hilo colgado esperando al otro.
        async with self._lock:
            self._model = synth
            # Una frase de verdad, no un «Hello.»: los núcleos de CUDA que se
            # compilan en la primera síntesis dependen de la longitud, y con un
            # calentamiento de una palabra la primera respuesta real seguía
            # pagando parte de ese coste y sonando peor que las siguientes.
            await asyncio.to_thread(
                self._synthesize,
                "Hi! I'm ready. Just tell me what you need and I'll take care of it."
                if self._language == "en" else
                "Hola, ya estoy lista. Dime qué necesitas y me encargo.")
        log.info("XTTS-v2 listo en %s", self._resolve_device())

    # --------------------------------------------------------------- síntesis
    def _synthesize(self, text: str, language: str | None = None) -> np.ndarray:
        gpt_latent, speaker = self._latents
        with self._torch.inference_mode():
            out = self._model.inference(
                text,
                language or self._language,
                gpt_latent,
                speaker,
                temperature=self._temperature,
                speed=self._speed,
                enable_text_splitting=False,   # el troceado ya lo hace plan_chunks
            )
        wav = out["wav"]
        if not isinstance(wav, np.ndarray):
            wav = np.asarray(wav, dtype=np.float32)
        return wav.astype(np.float32, copy=False)

    def _synthesize_checked(self, text: str, language: str | None) -> np.ndarray:
        """Sintetiza y, si el modelo se atasca, lo intenta otra vez.

        Medido sobre 40 síntesis en inglés: se atasca 1 vez (2 %), y el segundo
        intento la arregló. O sea que esto no es lo que sostiene la calidad —eso
        es hablar en inglés en vez de español— sino un seguro barato contra el
        único fallo que resulta insoportable de oír. El 98 % de las veces no
        cuesta nada; el 2 % restante cuesta una síntesis de más.

        Si los dos intentos salen atascados se usa el más corto, que es el menos
        desquiciante de los dos.
        """
        wav = self._synthesize(text, language)
        if not looks_degenerate(text, len(wav) / OUTPUT_RATE):
            return wav
        log.warning("la voz se atascó en «%s…»; reintento", text[:40])
        segundo = self._synthesize(text, language)
        if looks_degenerate(text, len(segundo) / OUTPUT_RATE):
            log.warning("segundo intento también atascado; me quedo con el más corto")
            return segundo if len(segundo) < len(wav) else wav
        return segundo

    def _synthesize_stream(self, text: str, language: str | None, push) -> float:
        """Sintetiza en streaming; `push(trozo)` recibe cada trozo (o None al
        final) desde este hilo. Devuelve los segundos generados."""
        gpt_latent, speaker = self._latents
        total = 0
        try:
            with self._torch.inference_mode():
                for chunk in self._model.inference_stream(
                    text, language or self._language, gpt_latent, speaker,
                    stream_chunk_size=_STREAM_CHUNK_TOKENS,
                    temperature=self._temperature, speed=self._speed,
                    enable_text_splitting=False,
                ):
                    if self._cancel.is_set():
                        break
                    wav = chunk.detach().float().cpu().numpy().reshape(-1).astype(np.float32)
                    total += len(wav)
                    push(wav)
        finally:
            push(None)
        return total / OUTPUT_RATE

    async def _speak_streamed(self, text: str, language: str | None, player: "_Player",
                              on_level) -> bool:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def push(chunk) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, chunk)

        async def generate() -> float:
            async with self._lock:
                if self._cancel.is_set():
                    push(None)
                    return 0.0
                return await asyncio.to_thread(self._synthesize_stream, text, language, push)

        generating = asyncio.create_task(generate())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                if self._cancel.is_set() or not await player.play(chunk, on_level):
                    return False
            seconds = await generating
            if looks_degenerate(text, seconds):
                # En streaming ya sonó; reintentar sería repetirlo. Se anota
                # para saber cuánto pasa.
                log.warning("la voz se atascó en «%s…» (%.1f s)", text[:40], seconds)
            return True
        finally:
            if not generating.done():
                self._cancel.set()
                await asyncio.gather(generating, return_exceptions=True)

    async def _synthesize_async(self, text: str, language: str | None = None) -> np.ndarray:
        # El lock existe porque hay dos síntesis en vuelo a la vez (la actual y
        # la adelantada) y una sola GPU: dejarlas competir no acelera nada y
        # multiplica la VRAM en uso.
        async with self._lock:
            if self._cancel.is_set():
                return np.zeros(0, dtype=np.float32)
            return await asyncio.to_thread(self._synthesize_checked, text, language)

    def _plan(self, text: str) -> list[str]:
        """Cómo se parte la respuesta antes de sintetizar. Es una decisión de
        calidad de voz, no de rendimiento, y por eso el valor por defecto no es
        el más rápido.

        Medido con la voz clonada, parecido de lo generado con la referencia:
            frase entera                    0,664
            partida por frases              0,566
            troceada en fragmentos cortos   0,541
        XTTS necesita contexto para fijar el timbre; con tres palabras sueltas
        se le va la voz y cada trozo suena a una persona ligeramente distinta.
        Las muestras que se aprobaron eran frases enteras, así que eso es lo que
        se sintetiza: la respuesta completa de una vez, partida sólo si pasa del
        límite del modelo (250 caracteres en inglés), y por frases.

        "streaming" es el modo rápido: primer trozo minúsculo para que empiece a
        sonar en un segundo, a costa del parecido. Está para quien prefiera eso.
        """
        if self._chunking == "streaming":
            return plan_chunks_streaming(text, first_max=40, growth=1.5, rest_max=180)
        return plan_whole(text, max_chars=230)

    async def speak(self, text: str, on_level=None, language: str | None = None) -> bool:
        """`language` sólo se usa para lo que Nova dice en otro idioma que el
        habitual —las confirmaciones, que van en el idioma del usuario."""
        if self._model is None:
            await self.warmup()
        self._cancel.clear()
        chunks = self._plan(text)
        if not chunks:
            return True

        player = _Player(self._output_device, self._cancel)
        pending: Optional[asyncio.Task] = None
        completed = False
        try:
            await player.prepare()
            for index, chunk in enumerate(chunks):
                if len(chunk) >= _STREAM_FROM_CHARS:
                    if pending is not None:
                        pending = None
                    if not await self._speak_streamed(chunk, language, player, on_level):
                        return False
                    continue
                # Frase corta: entera y comprobada; la siguiente corta se
                # adelanta mientras suena esta.
                if pending is None:
                    pending = asyncio.create_task(self._synthesize_async(chunk, language))
                audio = await pending
                pending = None
                if index + 1 < len(chunks) and len(chunks[index + 1]) < _STREAM_FROM_CHARS:
                    pending = asyncio.create_task(self._synthesize_async(chunks[index + 1], language))
                if audio.size == 0 or self._cancel.is_set():
                    return False
                if not await player.play(audio, on_level):
                    return False
            completed = True
            return True
        except Exception as exc:
            log.error("fallo de síntesis: %s", exc)
            return False
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
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
        self._latents = None
