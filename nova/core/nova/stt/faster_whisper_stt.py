"""Transcripción local con faster-whisper.

Dos decisiones que marcan la latencia real, medidas en esta máquina
(Ryzen 7 7445HS, 6 núcleos, RTX 3050):

  * Whisper rellena siempre su ventana a 30 s, así que el coste del encoder no
    depende de lo que dure la frase: `small` en CPU tarda ~1,7 s tanto para
    «hola» como para una orden entera. En la GPU baja a ~0,2 s. Por eso el
    arranque se esfuerza en encontrar CUDA (ver `_prepare_cuda_libs`) en lugar
    de resignarse a la CPU.
  * `vad_filter=True` recorta el silencio antes de decodificar. Además de ahorrar
    tiempo, es la defensa principal contra las muletillas de subtítulos que
    Whisper inventa cuando le llega audio sin habla clara (ver hallucinations.py).

Local y no en la nube porque tu clave es de DeepSeek (que no ofrece STT), porque
el audio no sale de la máquina y porque la latencia no depende de tu conexión.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

import numpy as np

from .base import Transcript
from .hallucinations import is_hallucination

log = logging.getLogger("nova.stt")

# Construir dos WhisperModel a la vez revienta dentro de huggingface_hub con
# «type object 'disabled_tqdm' has no attribute '_lock'»: su barra de progreso
# inicializa un cerrojo de clase de forma perezosa y no es reentrante entre
# hilos. Pasa justo cuando el arranque carga en paralelo el modelo de
# transcripción y el del spotter de palabra activadora, que es lo normal aquí.
# Serializar sólo la construcción cuesta un par de segundos al arrancar y no
# afecta a la latencia de uso: cargar es lo único que se solapa.
MODEL_LOAD_LOCK = threading.Lock()

_CUDA_READY = False
# Hay que quedarse con dos cosas o el arreglo se deshace solo:
#
#   * os.add_dll_directory() devuelve un objeto que, al destruirse, DESHACE la
#     adición. Descartarlo significa que el recolector de basura quita el
#     directorio de la búsqueda en cualquier momento posterior.
#   * CTranslate2 carga cuBLAS y cuDNN con LoadLibrary a secas, y esa llamada no
#     mira los directorios añadidos con AddDllDirectory. Añadirlos no basta.
#
# Lo que sí funciona es precargar las DLL nosotros: una vez están en el proceso,
# el LoadLibrary de CTranslate2 las encuentra por nombre entre los módulos ya
# cargados. Las dos listas existen para que ni los cookies ni los handles se
# recojan mientras dure el proceso.
_CUDA_DLL_COOKIES: list = []
_CUDA_DLL_HANDLES: list = []

# cuBLAS para las multiplicaciones y cuDNN para las convoluciones del encoder de
# Whisper. Cada una arrastra sus dependientes, que se resuelven con los
# directorios ya añadidos.
_CUDA_PRELOAD = ("cublasLt64_12.dll", "cublas64_12.dll", "cudnn64_9.dll")


def _prepare_cuda_libs() -> bool:
    """Deja utilizables las DLL de CUDA que instala pip. Devuelve si lo consiguió.

    `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` deja las librerías en
    site-packages/nvidia/<paquete>/bin, que no está en el PATH. Sin este paso
    CTranslate2 no las encuentra y cae a CPU pese a estar instaladas —y el error
    («Library cublas64_12.dll is not found») aparece en la primera inferencia,
    no al cargar el modelo, que es lo que lo hacía difícil de diagnosticar.
    """
    global _CUDA_READY
    if sys.platform != "win32":
        return True  # en Linux el paquete de CUDA suele venir con las libs
    if _CUDA_READY:
        return True

    try:
        import nvidia  # type: ignore
    except ImportError:
        nvidia = None  # el usuario puede tener CUDA instalado a mano, en el PATH

    if nvidia is not None:
        for base in nvidia.__path__:
            for sub in sorted(Path(base).glob("*/bin")):
                if sub.is_dir():
                    try:
                        _CUDA_DLL_COOKIES.append(os.add_dll_directory(str(sub)))
                    except OSError:
                        continue

    import ctypes

    for name in _CUDA_PRELOAD:
        try:
            _CUDA_DLL_HANDLES.append(ctypes.WinDLL(name))
        except OSError:
            # cuBLAS es imprescindible; cuDNN puede faltar en instalaciones
            # antiguas y CTranslate2 aún arranca para algunos modelos.
            if name.startswith("cublas64"):
                return False
    _CUDA_READY = True
    return True


def _cuda_usable() -> bool:
    """¿Hay GPU *y* están las librerías que CTranslate2 necesita para usarla?

    `get_cuda_device_count() > 0` sólo dice que hay una tarjeta. CTranslate2
    carga cuBLAS y cuDNN en la primera inferencia, no al construir el modelo, así
    que sin esas DLL el fallo aparece tarde y con un error opaco. Comprobarlo
    antes es más barato que descubrirlo a mitad del arranque.
    """
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() <= 0:
            return False
    except Exception:
        return False

    if _prepare_cuda_libs():
        return True
    log.warning(
        "hay GPU pero faltan las librerías de CUDA; se usará la CPU (transcribir "
        "tardará ~1,5 s en vez de ~0,2 s). Se arregla con: "
        "pip install nvidia-cublas-cu12 nvidia-cudnn-cu12"
    )
    return False


# Por debajo de esto no hay voz que transcribir, sólo el ruido de fondo de la
# sala. Sirve para no reintentar sin filtro cuando de verdad no se dijo nada.
_UMBRAL_VOZ = 0.004


def _tiene_voz(audio: np.ndarray) -> bool:
    """¿Hay algún tramo con energía de voz, aunque el conjunto sea flojo?"""
    if audio.size < 1600:
        return False
    ventana = 320
    bloques = audio[: audio.size - audio.size % ventana].reshape(-1, ventana)
    return bool(np.max(np.sqrt(np.mean(bloques ** 2, axis=1))) > _UMBRAL_VOZ)


# En GPU, pesos en int8 y cálculo en float16: medido con `medium`, ocupa 1,2 GB
# en vez de 2,1 y transcribe igual de rápido y de bien. En una tarjeta de 6 GB
# que comparte con la voz clonada, Electron, Discord y Chrome, ese gigabyte es
# la diferencia entre ir holgado y que el avatar se quede en blanco.
_CUDA_COMPUTE = "int8_float16"


def _resolve_device(preference: str) -> tuple[str, str]:
    if preference == "cuda":
        _prepare_cuda_libs()
        return "cuda", _CUDA_COMPUTE
    if preference == "cpu":
        return "cpu", "int8"
    if _cuda_usable():
        return "cuda", _CUDA_COMPUTE
    return "cpu", "int8"


class FasterWhisperStt:
    def __init__(self, model: str = "small", device: str = "auto",
                 compute_type: str = "auto", language: str = "es",
                 beam_size: int = 5, cpu_threads: int = 0,
                 cpu_model: str = "", vad_filter: bool = True,
                 initial_prompt: str = ""):
        self._language = language
        # Sesga el decodificador hacia el vocabulario que de verdad se usa aquí.
        # Es lo que convierte «Cierra disto» en «Cierra Discord»: sin esta pista
        # Whisper escribe lo que suena, y los nombres de aplicaciones en inglés
        # dichos por un hispanohablante suenan a cualquier cosa. Medido sobre
        # siete órdenes con audio distorsionado: 0,970 -> 0,990 de fidelidad.
        self._initial_prompt = initial_prompt.strip() or None
        self._beam_size = max(1, int(beam_size))
        self._cpu_threads = max(0, int(cpu_threads))
        self._vad_filter = bool(vad_filter)
        self._device, auto_compute = _resolve_device(device)
        self._compute_type = auto_compute if compute_type in ("", "auto", None) else compute_type
        # En CPU un modelo grande no es «un poco más lento»: es inusable en un
        # asistente de voz (1,7 s medidos con `small` para un simple «hola»). Si
        # no hay GPU se baja de modelo en vez de fingir que la latencia vale.
        self._model_name = model
        self._cpu_model = cpu_model or model
        if self._device == "cpu" and self._cpu_model != model:
            log.info("sin GPU: uso el modelo '%s' en vez de '%s' para no pasar de 0,5 s",
                     self._cpu_model, model)
            self._model_name = self._cpu_model
        self._model = None
        self._lock = asyncio.Lock()

    def _load_model(self, device: str, compute_type: str):
        from faster_whisper import WhisperModel

        kwargs: dict = {"device": device, "compute_type": compute_type}
        if device == "cpu" and self._cpu_threads:
            kwargs["cpu_threads"] = self._cpu_threads
        with MODEL_LOAD_LOCK:
            model = WhisperModel(self._model_name, **kwargs)
        # Primera inferencia de mentira, pero por el mismo camino que las de
        # verdad: mismo beam y dos segundos de audio con algo de energía. Con
        # ceros y beam por defecto, los núcleos del decodificador se compilaban
        # en la *primera frase real* y esa costaba 2 s en vez de 0,3. Además es
        # aquí donde CUDA falla si le faltan librerías, lo que permite reintentar
        # en CPU antes de que nadie note nada.
        ruido = np.random.default_rng(0).normal(0, 0.01, 32000).astype(np.float32)
        list(model.transcribe(ruido, language=self._language,
                              beam_size=self._beam_size, vad_filter=False,
                              condition_on_previous_text=False, temperature=0.0)[0])
        return model

    async def warmup(self) -> None:
        if self._model is not None:
            return

        log.info("cargando whisper %s en %s (%s)", self._model_name, self._device, self._compute_type)
        try:
            self._model = await asyncio.to_thread(self._load_model, self._device, self._compute_type)
        except Exception as exc:
            if self._device == "cpu":
                raise
            log.warning("whisper no pudo usar %s (%s); reintentando en CPU",
                        self._device, str(exc).splitlines()[0])
            self._device, self._compute_type = "cpu", "int8"
            self._model_name = self._cpu_model
            self._model = await asyncio.to_thread(self._load_model, "cpu", "int8")
        log.info("whisper listo: %s en %s", self._model_name, self._device)

    async def transcribe(self, pcm16: bytes, final: bool = True) -> Transcript:
        if self._model is None:
            await self.warmup()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size < 1600:  # < 100 ms
            return Transcript("", final)
        seconds = audio.size / 16000.0

        def _run(usar_vad: bool | None = None) -> str:
            segments, _ = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=self._beam_size if final else 1,
                vad_filter=self._vad_filter if usar_vad is None else usar_vad,
                # El VAD de la sesión ya cerró la frase; aquí sólo se recorta el
                # relleno de los extremos, con margen para no comerse la primera
                # sílaba ni la última.
                vad_parameters={"min_silence_duration_ms": 250, "speech_pad_ms": 150},
                condition_on_previous_text=False,
                temperature=0.0,
                # Los tres umbrales que hacen que un segmento sin habla se
                # descarte en vez de convertirse en una frase inventada.
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,
                initial_prompt=self._initial_prompt,
            )
            return " ".join(s.text.strip() for s in segments).strip()

        async with self._lock:
            text = await asyncio.to_thread(_run)
            # El filtro de silencio recorta antes de decodificar, y con un
            # micrófono flojo o ruidoso a veces recorta también la voz: la
            # transcripción sale vacía y Nova parece sorda. Si eso pasa pero el
            # audio tenía energía, se reintenta sin filtro. Sólo se paga en el
            # caso malo, y convierte «no me entiende» en «me entiende peor».
            if final and not text and self._vad_filter and _tiene_voz(audio):
                log.info("el filtro de silencio dejó la frase vacía; reintento sin él")
                text = await asyncio.to_thread(_run, False)

        if final and text and is_hallucination(text, audio_seconds=seconds):
            log.info("descarto transcripción inventada (%.1f s de audio): %r", seconds, text)
            return Transcript("", final, self._language)
        return Transcript(text, final, self._language)

    async def close(self) -> None:
        self._model = None
