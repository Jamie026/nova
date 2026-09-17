"""Detección de palabra activadora.

Tres motores detrás de la misma interfaz, en orden de lo que cuestan en reposo
—que es lo que importa, porque esto corre todo el día sin que pase nada:

* PorcupineWakeWord — ~1 % de CPU. Necesita una AccessKey gratuita de
  console.picovoice.ai. "Nova" no es un keyword integrado: se genera el .ppn
  gratis en la misma consola y se apunta wake.keyword_path a él.
* OpenWakeWordEngine — ~4 % de CPU medido aquí, **sin clave y sin registro**.
  Modelos ONNX preentrenados que se descargan solos la primera vez. La pega: no
  existe un modelo de "Nova", sólo un puñado de palabras fijas, de las cuales
  "hey jarvis" es la que mejor le pega a esto.
* WhisperKeywordSpotter — el único que reconoce "Nova" dicho en español, y por
  eso el más útil aquí pese a ser el más caro: transcribe una ventana entera cada
  pocas décimas. En CPU eso llega al 115 % de carga (o sea, no da abasto y se le
  escapan activaciones); en GPU se queda en el 25 %, que ya es perfectamente
  usable. De ahí que wake.device sea "auto" y no "cpu".

Cuál elegir depende de cómo pronuncies, no de cuál es "mejor": el modelo de
"hey jarvis" está entrenado con fonética inglesa y, medido aquí, pasa de 0,993
con acento nativo a 0,220 diciendo "Jarvis" con J española. Por debajo de 0,5 no
despierta nunca.

Ningún motor envía audio a Internet. El micrófono sólo sale de la máquina cuando
el usuario ya activó al agente.
"""
from __future__ import annotations

import logging
import os
from difflib import SequenceMatcher
from typing import Optional, Protocol

import numpy as np

from .format import FRAME_BYTES, SAMPLE_RATE

log = logging.getLogger("nova.wake")

_PUNCTUATION = str.maketrans({c: " " for c in ",.;:!?¡¿\"'«»()-–—…"})


def _clean(text: str) -> str:
    """Minúsculas, sin acentos, sin puntuación y con espacios colapsados.

    Whisper devuelve puntuación y mayúsculas («¿Nova?», «Nova.»), y en español
    escribe tildes donde el usuario no las dijo. Todo eso es ruido para comparar.
    """
    table = str.maketrans("áéíóúüñ", "aeiouun")
    return " ".join(text.lower().translate(table).translate(_PUNCTUATION).split())


class WakeWordEngine(Protocol):
    frame_bytes: int
    # True si process() tarda lo suficiente como para bloquear el bucle de
    # eventos. La sesión lo usa para decidir si llamarlo en línea o en un hilo:
    # despachar 100 veces por segundo al pool cuesta más que el propio Porcupine.
    blocking: bool

    def process(self, frame: bytes) -> bool: ...
    def close(self) -> None: ...


class PorcupineWakeWord:
    def __init__(self, keyword_path: str = "", builtin_fallback: str = "jarvis",
                 sensitivity: float = 0.6, access_key: str = ""):
        import pvporcupine

        access_key = access_key or os.environ.get("PICOVOICE_ACCESS_KEY", "")
        if not access_key:
            raise RuntimeError("Falta PICOVOICE_ACCESS_KEY en el entorno")

        if keyword_path and os.path.exists(keyword_path):
            self._engine = pvporcupine.create(
                access_key=access_key, keyword_paths=[keyword_path], sensitivities=[sensitivity]
            )
            log.info("wake word personalizada: %s", os.path.basename(keyword_path))
        else:
            if keyword_path:
                log.warning("no existe %s; uso el keyword integrado '%s'", keyword_path, builtin_fallback)
            self._engine = pvporcupine.create(
                access_key=access_key, keywords=[builtin_fallback], sensitivities=[sensitivity]
            )
            log.info("wake word integrada: %s", builtin_fallback)

        self.frame_bytes = self._engine.frame_length * 2
        self.blocking = False  # ~0,1 ms por trama en C: sale más barato en línea
        self._buffer = bytearray()

    def process(self, frame: bytes) -> bool:
        self._buffer.extend(frame)
        detected = False
        while len(self._buffer) >= self.frame_bytes:
            chunk = bytes(self._buffer[: self.frame_bytes])
            del self._buffer[: self.frame_bytes]
            pcm = np.frombuffer(chunk, dtype=np.int16)
            if self._engine.process(pcm) >= 0:
                detected = True
                self._buffer.clear()
                break
        return detected

    def close(self) -> None:
        try:
            self._engine.delete()
        except Exception:
            pass


class OpenWakeWordEngine:
    """Alternativa sin clave ni registro, con modelos ONNX preentrenados.

    Trabaja en tramas de 1280 muestras (80 ms) a 16 kHz, que es lo que espera su
    extractor de características; se acumulan las tramas de 10 ms del micrófono
    hasta completar una. Medido en esta máquina: 3,6 ms por trama, o sea ~4,5 %
    de un núcleo en reposo. Eso es una vigésima parte de lo que gasta transcribir
    con Whisper, y por eso es el sustituto natural de Porcupine mientras no haya
    AccessKey.

    Tras detectar se llama a `reset()`: sin eso el búfer interno sigue conteniendo
    la palabra y vuelve a dispararse en las siguientes tramas.
    """

    FRAME_SAMPLES = 1280

    def __init__(self, model: str = "hey_jarvis", threshold: float = 0.5):
        import openwakeword
        from openwakeword.model import Model

        # Idempotente y con caché en disco: sólo descarga la primera vez.
        try:
            openwakeword.utils.download_models([model])
        except Exception as exc:
            log.debug("no se pudieron descargar/verificar los modelos: %s", exc)

        self._model = Model(wakeword_models=[model], inference_framework="onnx")
        self._name = model
        self._threshold = float(threshold)
        self.frame_bytes = self.FRAME_SAMPLES * 2
        self.blocking = False  # 3,6 ms cada 80 ms de audio: no merece un hilo
        self._buffer = bytearray()
        log.info("wake word sin clave (openWakeWord): '%s', umbral %.2f",
                 model.replace("_", " "), self._threshold)

    def process(self, frame: bytes) -> bool:
        self._buffer.extend(frame)
        while len(self._buffer) >= self.frame_bytes:
            chunk = bytes(self._buffer[: self.frame_bytes])
            del self._buffer[: self.frame_bytes]
            scores = self._model.predict(np.frombuffer(chunk, dtype=np.int16))
            if max(scores.values(), default=0.0) >= self._threshold:
                self._buffer.clear()
                self._model.reset()
                return True
        return False

    def close(self) -> None:
        self._model = None


class WhisperKeywordSpotter:
    """Transcribe ventanas cortas y busca la palabra dentro.

    Reconoce cualquier palabra —incluida "Nova" dicha en español, que es lo que
    ninguno de los otros dos hace sin pagar o sin registrarse. A cambio decodifica
    una ventana de más de un segundo varias veces por segundo. En la GPU eso son
    unas décimas y se queda en el 25 % de carga; en CPU no llega a tiempo.

    El detalle que decide si funciona o no: Whisper transcribe fonéticamente lo
    que oye, y «Nova» dicho en español sale muchísimas veces como **«no va»**, en
    dos palabras, o como «Nova.», «¿Nova?», «Noba». Comparar palabra a palabra
    contra «nova» falla en todos esos casos («no» contra «nova» da 0,67, por
    debajo de cualquier umbral razonable). Por eso se compara también contra el
    texto con los espacios quitados y contra los pares de palabras consecutivas:
    «no»+«va» → «nova» → coincidencia exacta.
    """

    def __init__(self, keyword: str = "nova", model_size: str = "tiny",
                 window_ms: int = 1200, threshold: float = 0.72,
                 device: str = "cpu"):
        from faster_whisper import WhisperModel

        from ..stt.faster_whisper_stt import MODEL_LOAD_LOCK, _resolve_device

        resolved, compute = _resolve_device(device)
        self.frame_bytes = FRAME_BYTES
        self.blocking = True  # decodifica con Whisper: obligatorio sacarlo del bucle
        self.set_keywords(keyword)
        self._threshold = threshold
        self._window_bytes = SAMPLE_RATE * 2 * window_ms // 1000
        self._buffer = bytearray()
        # El mismo cerrojo que usa el motor de transcripción: el arranque carga
        # los dos modelos en paralelo y huggingface_hub no lo aguanta.
        with MODEL_LOAD_LOCK:
            self._model = WhisperModel(model_size, device=resolved, compute_type=compute)
        self._energy_gate = 0.012
        log.info("spotter local por transcripción, palabra '%s' (%s en %s)",
                 keyword, model_size, resolved)

    def set_keywords(self, keywords: str) -> None:
        """Una o varias palabras separadas por comas: «corrin, nova».

        Se puede llamar en caliente: el spotter sólo compara texto, así que
        cambiar la palabra desde el panel no necesita reiniciar nada. Antes sí
        hacía falta y el usuario cambió a «corrin», siguió diciendo «Nova» sin
        reiniciar, y creyó que el cambio no había funcionado.
        """
        self._keywords = [_clean(k) for k in keywords.split(",") if _clean(k)] or ["nova"]
        self._keyword = self._keywords[0]
        # La pista para el decodificador: con ella Whisper escribe «Corrin» donde
        # sin ella escribía «Colin», «Coring» o «por en». Medido en las ventanas
        # que fallaban.
        self._prompt = "Palabra de activación: " + ", ".join(
            k.capitalize() for k in self._keywords) + "."

    def _matches(self, text: str) -> bool:
        cleaned = _clean(text)
        if not cleaned:
            return False
        words = cleaned.split()
        joined = cleaned.replace(" ", "")
        candidates = list(words) + [a + b for a, b in zip(words, words[1:])]
        for keyword in self._keywords:
            # 1. El texto entero sin espacios: cubre «no va», «no-va» y «no va.»
            if keyword in joined:
                return True
            # 2. Palabras sueltas y pares consecutivos, con tolerancia a errores.
            for candidate in candidates:
                if SequenceMatcher(None, candidate, keyword).ratio() >= self._threshold:
                    return True
        return False

    def process(self, frame: bytes) -> bool:
        self._buffer.extend(frame)
        if len(self._buffer) < self._window_bytes:
            return False
        window = bytes(self._buffer[-self._window_bytes:])
        del self._buffer[: self._window_bytes // 2]  # 50 % de solape

        audio = np.frombuffer(window, dtype=np.int16).astype(np.float32) / 32768.0
        if float(np.sqrt(np.mean(audio ** 2))) < self._energy_gate:
            return False  # silencio: no gastamos CPU en decodificar

        segments, _ = self._model.transcribe(audio, language="es", beam_size=1, vad_filter=False,
                                             initial_prompt=self._prompt)
        text = " ".join(s.text for s in segments).strip()
        if not text:
            return False
        # Con NOVA_LOG_LEVEL=DEBUG se ve exactamente qué oyó: es la única forma
        # razonable de ajustar la palabra activadora a tu voz y a tu micrófono.
        log.debug("spotter oyó: %r", text)
        return self._matches(text)

    def close(self) -> None:
        self._model = None


class NullWakeWord:
    frame_bytes = FRAME_BYTES
    blocking = False

    def process(self, frame: bytes) -> bool:
        return False

    def close(self) -> None:
        pass


def _build_porcupine(config) -> WakeWordEngine:
    return PorcupineWakeWord(
        keyword_path=config.get("wake.keyword_path", ""),
        builtin_fallback=config.get("wake.builtin_fallback", "jarvis"),
        sensitivity=float(config.get("wake.sensitivity", 0.6)),
    )


def _build_openwakeword(config) -> WakeWordEngine:
    return OpenWakeWordEngine(
        model=config.get("wake.oww_model", "hey_jarvis"),
        threshold=float(config.get("wake.oww_threshold", 0.5)),
    )


def _build_whisper(config) -> WakeWordEngine:
    return WhisperKeywordSpotter(
        keyword=config.get("wake.keyword", "nova"),
        model_size=config.get("wake.model", "tiny"),
        window_ms=int(config.get("wake.window_ms", 1200)),
        threshold=float(config.get("wake.threshold", 0.72)),
        device=config.get("wake.device", "cpu"),
    )


_BUILDERS = {
    "porcupine": _build_porcupine,
    "openwakeword": _build_openwakeword,
    "whisper": _build_whisper,
}

# Si el motor elegido no se puede construir se prueba el siguiente, de menos a
# más caro. Antes se caía directamente de Porcupine a Whisper —el más pesado de
# los tres— sólo por no tener una clave gratuita.
_FALLBACK = ("porcupine", "openwakeword", "whisper")


def build_wake_word(config) -> Optional[WakeWordEngine]:
    engine = config.get("wake.engine", "porcupine")
    if engine == "off":
        return NullWakeWord()

    order = [engine] + [name for name in _FALLBACK if name != engine]
    for index, name in enumerate(order):
        builder = _BUILDERS.get(name)
        if builder is None:
            continue
        try:
            return builder(config)
        except Exception as exc:
            siguiente = order[index + 1] if index + 1 < len(order) else "ninguno"
            log.warning("%s no disponible (%s); paso a %s", name, exc, siguiente)
    log.error("no hay detector de wake word disponible; usa Ctrl+Shift+Espacio")
    return NullWakeWord()
