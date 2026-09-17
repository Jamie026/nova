"""Sesión de voz: el bucle que une micrófono, wake word, VAD, STT, agente y TTS.

Un único consumidor lee las tramas del micrófono y las reparte según el estado.
Eso evita que dos componentes compitan por el dispositivo y hace que el barge-in
sea trivial: mientras el agente habla, el mismo bucle sigue pasando tramas al VAD.

Ciclo de vida de una interacción:
  IDLE ──wake word──► WAKE_WORD_DETECTED ──► LISTENING
  LISTENING ──fin de voz──► THINKING ──herramientas──► EXECUTING ──► SPEAKING
  SPEAKING ──► LISTENING (ventana de seguimiento) ──silencio──► SLEEPING ──► IDLE
  SPEAKING + voz del usuario ──► corta el TTS ──► LISTENING
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any, Optional

from .agent import consent, dismissal
from .agent.orchestrator import Orchestrator
from .audio.format import FRAME_BYTES, FRAME_MS
from .audio.mic import MicrophoneStream, frame_rms, rms_level
from .audio.vad import VadConfig, VadEvent, VoiceActivityDetector
from .audio.wakeword import build_wake_word
from .events import (assistant_text, confirm_request, confirm_resolved, error, mic_level, state_event,
                     status, transcript)
from .state import AgentState, StateMachine

log = logging.getLogger("nova.session")

_SILENT_FRAME = bytes(FRAME_BYTES)
_ECHO_CALIBRATION_S = 1.0     # primer segundo de audio de cada respuesta: se mide el eco
_ECHO_MARGIN = 1.8            # para interrumpir hay que superar el eco por este factor
# Si la voz no carga (típicamente: Windows sin memoria de compromiso), se
# reintenta con estas esperas y luego se deja de insistir. Mientras tanto Nova
# contesta por escrito.
_TTS_RETRY_DELAYS_S = (30, 60, 120, 300, 600)
_WINERROR_COMMIT_LIMIT = 1455   # «El archivo de paginación es demasiado pequeño»


class VoiceSession:
    def __init__(self, config, ipc, stt, tts, orchestrator: Orchestrator, machine: StateMachine):
        self._config = config
        self._ipc = ipc
        self._stt = stt
        self._tts = tts
        self._agent = orchestrator
        self._machine = machine

        self._mic = MicrophoneStream(config.get("audio.input_device"))
        self._wake = None
        self._vad = VoiceActivityDetector(VadConfig(
            aggressiveness=int(config.get("vad.aggressiveness", 2)),
            start_ms=int(config.get("vad.start_ms", 150)),
            end_ms=int(config.get("vad.end_ms", 800)),
        ))
        # VAD aparte para interrumpir a Nova mientras habla. Necesita más voz
        # seguida que el normal y, sobre todo, más VOLUMEN: ver _handle_barge_in.
        self._barge_vad = VoiceActivityDetector(VadConfig(
            aggressiveness=3,
            start_ms=int(config.get("session.barge_in_start_ms", 400)),
            end_ms=int(config.get("vad.end_ms", 800)),
        ))
        self._barge_min_level = float(config.get("session.barge_in_min_level", 0.04))
        # Eco medido en cada respuesta: ver _handle_barge_in.
        self._audio_since: float | None = None
        self._echo_samples: list[float] = []
        self._echo_level = 0.0

        self._utterance = bytearray()
        # Mientras Nova piensa o ejecuta, el micrófono sólo busca un «para» o
        # un «descansa»: ver _handle_working.
        self._side = bytearray()
        self._side_capturing = False
        self._side_task: Optional[asyncio.Task] = None
        # Colchón de tramas anteriores al inicio de voz. Sin esto se pierde el
        # principio de cada frase: el VAD necesita `start_ms` de voz seguida para
        # decidir que alguien habló, y esas tramas —las que contienen la primera
        # sílaba— quedaban fuera de la grabación. «Abre Spotify» llegaba a Whisper
        # como «Spotify». Era el motivo real de que Nova «no entendiera nada».
        self._preroll: collections.deque[bytes] = collections.deque(
            maxlen=max(1, int(config.get("vad.preroll_ms", 400)) // FRAME_MS))
        self._capturing = False
        self._utterance_started = 0.0
        self._last_partial = 0.0
        self._follow_up_until = 0.0
        self._turn_task: Optional[asyncio.Task] = None
        self._partial_task: Optional[asyncio.Task] = None
        self._wake_task: Optional[asyncio.Task] = None
        self._pending_confirms: dict[str, asyncio.Future] = {}
        # Mientras hay una confirmación pendiente y la pregunta ya sonó, el
        # micrófono va a buscar un «sí» o un «no» en vez de una orden.
        self._confirm_listening = False
        self._confirm_id: str | None = None
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None
        self._tts_warmup_task: Optional[asyncio.Task] = None

        self._max_utterance_bytes = 32 * int(config.get("vad.max_utterance_ms", 20000))

    # ------------------------------------------------------------------ arranque
    async def start(self) -> None:
        self._machine.on_change(self._on_state_change)
        # Los arranques caros, a la vez: ninguno depende de los otros. Cargar
        # Whisper en la GPU son ~12 s, el detector otros ~5, y la conexión con la
        # API cabe entera dentro de esa espera.
        checker = getattr(self._stt, "check_speech", None)
        if checker is not None and hasattr(self._tts, "verifier"):
            self._tts.verifier = checker
        self._wake, *_ = await asyncio.gather(
            asyncio.to_thread(build_wake_word, self._config),
            self._stt.warmup(),
            self._agent.warmup(),
        )
        # La voz arranca DESPUÉS de Whisper y no se espera: si el usuario habla
        # antes de que termine, `speak()` espera lo que le quede. Se probó a
        # lanzarla en paralelo para ganar 12 s y salió caro: CTranslate2 y
        # PyTorch inicializando CUDA a la vez en dos hilos colgaban el arranque
        # o lo mataban con un access violation (código 3221225477), dos de
        # cada dos veces un día y ninguna el anterior. En serie no falla.
        self._tts_warmup_task = asyncio.create_task(self._warmup_tts(), name="tts-warmup")
        await self._mic.start()
        self._running = True
        self._loop_task = asyncio.create_task(self._audio_loop(), name="audio-loop")
        await self._machine.to(AgentState.IDLE)
        log.info("Nova en escucha pasiva. Di la palabra activadora para empezar.")

    async def _speak(self, text: str, language: str | None = None) -> bool:
        """Habla, indicando idioma si el motor sabe cambiarlo por frase.
        Sin voz (no cargó), no falla: la pregunta ya está en pantalla."""
        try:
            if language is None:
                return await self._tts.speak(text)
            try:
                return await self._tts.speak(text, language=language)
            except TypeError:
                return await self._tts.speak(text)   # motor de un solo idioma
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("sin voz para «%s»: %s", text[:40], exc)
            return False

    async def _warmup_tts(self) -> None:
        """El motor de voz local sube pesos a la GPU; el de red no tiene nada que
        calentar. De ahí el getattr en vez de exigirlo en el Protocol.

        Si falla se reintenta más tarde: la causa vista es Windows sin memoria
        de compromiso (RAM + archivo de paginación) con un juego, Discord y el
        navegador abiertos, y eso se arregla solo cuando algo se cierra. Sin
        reintento, la voz se quedaba muerta hasta reiniciar Nova.
        """
        warm = getattr(self._tts, "warmup", None)
        if warm is None:
            return
        for attempt, delay in enumerate((0,) + _TTS_RETRY_DELAYS_S):
            if delay:
                await asyncio.sleep(delay)
            try:
                await warm()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._explain_voice_failure(exc)
                if attempt < len(_TTS_RETRY_DELAYS_S):
                    log.warning("la voz no pudo precargarse (intento %d): %s — reintento en %d s",
                                attempt + 1, exc, _TTS_RETRY_DELAYS_S[attempt])
                else:
                    log.error("la voz no pudo precargarse tras %d intentos: %s. Nova sigue, "
                              "por escrito.", attempt + 1, exc)

    @staticmethod
    def _explain_voice_failure(exc: BaseException) -> None:
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) == _WINERROR_COMMIT_LIMIT:
            log.error("Windows se quedó sin memoria de compromiso (RAM + archivo de paginación) "
                      "al cargar la voz. Cierra algo grande (el juego, pestañas del navegador) "
                      "o agranda el archivo de paginación: ver INSTRUCCIONES §9, «error 1455».")

    async def stop(self) -> None:
        self._running = False
        for task in (self._loop_task, self._wake_task, self._partial_task,
                     self._tts_warmup_task):
            if task and not task.done():
                task.cancel()
        await self._cancel_turn()
        await self._mic.stop()
        await self._tts.close()
        await self._stt.close()
        if self._wake:
            self._wake.close()

    async def _on_state_change(self, _previous: AgentState, new: AgentState) -> None:
        if new is AgentState.SPEAKING:
            self._barge_vad.reset()
            self._preroll.clear()
            self._audio_since = None
            self._echo_samples = []
            self._echo_level = 0.0
        await self._ipc.broadcast(state_event(new.value))

    # -------------------------------------------------------------- bucle de audio
    async def _audio_loop(self) -> None:
        level_tick = 0
        while self._running:
            try:
                frame = await asyncio.wait_for(self._mic.frames.get(), timeout=1.0)
            except asyncio.TimeoutError:
                await self._check_follow_up_timeout()
                continue
            except asyncio.CancelledError:
                return

            state = self._machine.state
            level_tick += 1
            if level_tick % 5 == 0 and state in (AgentState.LISTENING, AgentState.SPEAKING):
                self._ipc.broadcast_soon(mic_level(rms_level(frame)))

            try:
                if self._confirm_id is not None:
                    await self._handle_confirm_audio(frame)
                elif state in (AgentState.IDLE, AgentState.SLEEPING):
                    await self._handle_idle(frame)
                elif state is AgentState.LISTENING:
                    await self._handle_listening(frame)
                elif state is AgentState.SPEAKING and self._config.get("session.barge_in", True):
                    await self._handle_barge_in(frame)
                elif state in (AgentState.THINKING, AgentState.EXECUTING):
                    await self._handle_working(frame)
            except Exception:
                log.exception("fallo en el bucle de audio")
                await self._machine.to(AgentState.ERROR)
                await self._ipc.broadcast(error("Error procesando audio"))
                await asyncio.sleep(0.5)
                await self._machine.to(AgentState.IDLE)

    async def _handle_idle(self, frame: bytes) -> None:
        if self._wake is None:
            return
        if not self._wake.blocking:
            if self._wake.process(frame):
                await self._on_wake_detected()
            return
        # Motor pesado: una sola detección en vuelo; las tramas que lleguen
        # mientras tanto se descartan en vez de encolarse sin límite.
        if self._wake_task and not self._wake_task.done():
            return

        async def _detect(payload: bytes) -> None:
            if await asyncio.to_thread(self._wake.process, payload):
                await self._on_wake_detected()

        self._wake_task = asyncio.create_task(_detect(frame), name="wake-detect")

    async def _on_wake_detected(self) -> None:
        if self._machine.state not in (AgentState.IDLE, AgentState.SLEEPING):
            return
        log.info("wake word detectada")
        await self._machine.to(AgentState.WAKE_WORD_DETECTED)
        await self._begin_listening(greet=True)

    async def _handle_listening(self, frame: bytes) -> None:
        event = self._vad.push(frame)

        if event is VadEvent.SPEECH_START and not self._capturing:
            self._capturing = True
            self._utterance.clear()
            # Lo primero que entra es lo que ya había sonado antes de que el VAD
            # se diera cuenta.
            self._utterance.extend(b"".join(self._preroll))
            self._preroll.clear()
            self._utterance_started = time.monotonic()
            self._last_partial = time.monotonic()
            await self._ipc.broadcast(status("Escuchando..."))
        elif not self._capturing:
            self._preroll.append(frame)

        if self._capturing:
            self._utterance.extend(frame)
            if len(self._utterance) > self._max_utterance_bytes:
                await self._finish_utterance()
                return
            if self._config.get("stt.partials", False):
                interval = int(self._config.get("stt.partial_interval_ms", 900)) / 1000
                if time.monotonic() - self._last_partial > interval and len(self._utterance) > 16000:
                    self._last_partial = time.monotonic()
                    self._schedule_partial(bytes(self._utterance))

        if event is VadEvent.SPEECH_END and self._capturing:
            await self._finish_utterance()
            return

        await self._check_follow_up_timeout()

    async def _handle_barge_in(self, frame: bytes) -> None:
        """Interrumpir a Nova cuando el usuario habla encima, y sólo entonces.

        El micrófono de un portátil oye los altavoces del portátil. Con el mismo
        VAD que en la escucha normal, Nova se interrumpía A SÍ MISMA: su propia
        voz por los altavoces contaba como «el usuario habla», cortaba la
        respuesta, y Whisper transcribía un trozo de inglés como basura en
        español («¡Suscríbete!», «Con.», «Mi primer tal.»). Siete veces en una
        sesión, siempre entre uno y cuatro segundos después de empezar a hablar.

        Dos defensas, porque el eco y la voz de verdad se distinguen por el
        volumen y por la duración:
          * Las tramas por debajo de `barge_in_min_level` se le pasan al VAD como
            silencio. El eco de los altavoces llega al micrófono a una fracción
            del nivel al que llega la voz de quien está delante (medido en este
            portátil: voz ~0,12 de RMS; el umbral, 0,04).
          * Hacen falta 400 ms de voz seguida y no 150: un golpe de tos o un
            pico de eco no llegan; una frase sí.
        El colchón de tramas anteriores se mantiene también aquí, para que
        interrumpir no se coma la primera sílaba de lo que dice el usuario.
        """
        level = frame_rms(frame)
        threshold = self._barge_threshold(level)
        loud = level >= threshold
        self._preroll.append(frame)
        event = self._barge_vad.push(frame if loud else _SILENT_FRAME)
        if event is VadEvent.SPEECH_START:
            log.info("barge-in: el usuario interrumpe (nivel %.3f, umbral %.3f)", level, threshold)
            preroll = b"".join(self._preroll)
            await self._tts.stop()
            await self._cancel_turn()
            await self._begin_listening(greet=False, drain=False)
            self._capturing = True
            self._utterance.extend(preroll)  # incluye la trama que disparó la interrupción
            self._utterance_started = time.monotonic()
            self._last_partial = time.monotonic()

    def _barge_threshold(self, level: float) -> float:
        """Nivel a partir del cual una trama cuenta como voz encima de Nova.

        El umbral fijo dependía del volumen de los altavoces: subido el
        volumen, el eco pasaba de 0,04 y Nova se interrumpía sola (y Whisper
        transcribía su inglés como «¡Adiós!»). Ahora el primer segundo de
        audio de cada respuesta mide cuánto eco llega, y para interrumpir
        hace falta superar 1,8 veces eso. Sin audio sonando, vale el fijo.
        """
        if self._audio_since is None:
            return self._barge_min_level
        elapsed = time.monotonic() - self._audio_since
        if elapsed < _ECHO_CALIBRATION_S:
            if not self._echo_samples:
                self._barge_vad.reset()   # lo acumulado antes del audio no cuenta
            self._echo_samples.append(level)
            return 10.0          # calibrando: no se interrumpe
        if self._echo_samples and not self._echo_level:
            ordered = sorted(self._echo_samples)
            self._echo_level = ordered[int(len(ordered) * 0.9)]
            log.debug("eco de los altavoces: %.3f (umbral %.3f)", self._echo_level,
                      max(self._barge_min_level, self._echo_level * _ECHO_MARGIN))
        return max(self._barge_min_level, self._echo_level * _ECHO_MARGIN)

    async def _handle_working(self, frame: bytes) -> None:
        """Audio mientras Nova piensa o ejecuta herramientas.

        Antes el micrófono se ignoraba en estos estados: un «descansa» o un
        «para» en mitad de una cadena de herramientas no llegaba a ningún
        sitio. Ahora se escucha con el mismo filtro que el barge-in (volumen
        y duración), pero lo oído sólo cuenta si es una orden de parar o de
        despedirse: cualquier otra cosa (el usuario hablando con alguien) se
        ignora y la tarea sigue. Cortar una cadena de herramientas a medias
        por una conversación ajena sería peor que no oír.
        """
        level = frame_rms(frame)
        loud = level >= self._barge_threshold(level)
        if not self._side_capturing:
            self._preroll.append(frame)
        event = self._barge_vad.push(frame if loud else _SILENT_FRAME)
        if event is VadEvent.SPEECH_START and not self._side_capturing:
            self._side_capturing = True
            self._side = bytearray(b"".join(self._preroll))
            self._preroll.clear()
        if not self._side_capturing:
            return
        self._side.extend(frame)
        # «Para» o «descansa» no duran cuatro segundos: si se pasa, se cierra
        # y se interpreta lo que haya.
        if event is VadEvent.SPEECH_END or len(self._side) > 32 * 4000:
            audio = bytes(self._side)
            self._side_capturing = False
            self._side.clear()
            self._barge_vad.reset()
            if self._side_task is None or self._side_task.done():
                self._side_task = asyncio.create_task(self._check_side_utterance(audio),
                                                      name="stt-side")

    async def _check_side_utterance(self, audio: bytes) -> None:
        if len(audio) < 6400:   # < 200 ms: ruido
            return
        result = await self._stt.transcribe(audio, final=True, strict=False)
        text = result.text.strip()
        if not text:
            return
        kind = dismissal.interpret(text)
        if kind is None or self._machine.state not in (AgentState.THINKING, AgentState.EXECUTING,
                                                       AgentState.SPEAKING):
            log.info("oído mientras trabajaba (ignorado): %s", text)
            return
        await self._ipc.broadcast(transcript(text, partial=False))
        await self._dismiss(kind, text)

    # ----------------------------------------------------------------- turnos
    async def _begin_listening(self, greet: bool, drain: bool = True) -> None:
        self._vad.reset()
        self._capturing = False
        self._utterance.clear()
        self._side_capturing = False
        self._side.clear()
        # Al interrumpir NO se vacía la cola: esas tramas son el principio de lo
        # que el usuario está diciendo ahora mismo y perderlas se come la primera
        # palabra.
        if drain:
            self._mic.drain()
        # El colchón se vacía siempre: al despertar contiene la propia palabra
        # activadora, y al encadenar turnos contiene el final de lo que dijo Nova.
        self._preroll.clear()
        self._follow_up_until = time.monotonic() + float(self._config.get("session.follow_up_seconds", 12))
        await self._machine.to(AgentState.LISTENING)
        await self._ipc.broadcast(status("Escuchando..."))
        if greet and self._config.get("session.speak_greeting", False):
            asyncio.create_task(self._tts.speak(self._config.get("session.greeting", "Te escucho.")))

    def _schedule_partial(self, pcm: bytes) -> None:
        if self._partial_task and not self._partial_task.done():
            return

        async def _work() -> None:
            result = await self._stt.transcribe(pcm, final=False)
            if result.text and self._machine.state is AgentState.LISTENING:
                await self._ipc.broadcast(transcript(result.text, partial=True))

        self._partial_task = asyncio.create_task(_work(), name="stt-partial")

    async def _finish_utterance(self) -> None:
        audio = bytes(self._utterance)
        self._capturing = False
        self._utterance.clear()
        self._vad.reset()

        if len(audio) < 6400:  # < 200 ms: ruido, no una orden
            return

        await self._machine.to(AgentState.THINKING)
        await self._ipc.broadcast(status("Pensando..."))
        result = await self._stt.transcribe(audio, final=True)
        text = result.text.strip()
        log.info("transcripción final: %s", text or "(vacía)")
        if not text:
            await self._begin_listening(greet=False)
            return
        await self._ipc.broadcast(transcript(text, partial=False))
        await self.handle_text(text)

    async def handle_text(self, text: str) -> None:
        """Punto de entrada único de un turno: lo usan la voz y el panel de texto."""
        await self._cancel_turn()
        kind = dismissal.interpret(text)
        if kind is not None:
            await self._dismiss(kind, text)
            return
        self._turn_task = asyncio.create_task(self._run_turn(text), name="agent-turn")

    async def _dismiss(self, kind: str, heard: str) -> None:
        """«Descansa» → se calla, se despide en dos palabras y se duerme.
        «Para» → se calla, corta lo que estuviera haciendo y vuelve a escuchar.

        Ninguna de las dos pasa por el modelo: son las dos órdenes que tienen
        que obedecerse al instante, y el modelo tardaba dos segundos en
        contestar con una despedida de cinco.
        """
        await self._cancel_turn()
        if kind == dismissal.STOP:
            log.info("el usuario manda parar: «%s»", heard)
            await self._ipc.broadcast(assistant_text("Vale, paro."))
            await self._begin_listening(greet=False)
            return
        log.info("el usuario despide a Nova: «%s»", heard)
        farewell = str(self._config.get("session.farewell", "Okay, resting."))
        await self._ipc.broadcast(assistant_text("Descansando.", farewell))
        if farewell:
            await self._machine.to(AgentState.SPEAKING)
            try:
                await self._tts.speak(farewell)
            except Exception as exc:
                log.warning("no pude despedirme: %s", exc)
        await self._machine.to(AgentState.SLEEPING)
        await self._ipc.broadcast(status(""))
        await asyncio.sleep(0.2)
        await self._machine.to(AgentState.IDLE)

    async def _run_turn(self, text: str) -> None:
        """Un turno: modelo → (herramientas) → voz.

        La voz no espera a que el modelo termine. En cuanto la parte hablada
        está completa —el modelo escribe primero el inglés y luego el
        subtítulo— arranca la síntesis, y el subtítulo llega mientras ya
        suena. Medido, eso quita la mitad del tiempo del modelo de la espera.
        """
        started = time.monotonic()
        early: asyncio.Task | None = None
        early_text = ""
        first_audio = [0.0]

        def on_level(value: float) -> None:
            if not first_audio[0]:
                first_audio[0] = time.monotonic()
                self._audio_since = first_audio[0]
            self._ipc.broadcast_soon(mic_level(value))

        async def say(spoken: str) -> bool:
            # Si la voz aún está cargando, se espera PENSANDO y no HABLANDO:
            # el avatar estuvo «hablando» 70 s sin decir nada mientras
            # Chatterbox cargaba (y al final ni cargó).
            if getattr(self._tts, "ready", True) is False:
                await self._ipc.broadcast(status("Preparando la voz..."))
            try:
                warm = getattr(self._tts, "warmup", None)
                if warm is not None and getattr(self._tts, "ready", True) is False:
                    await warm()
                await self._machine.to(AgentState.SPEAKING)
                return await self._tts.speak(spoken, on_level=on_level)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Sin voz no se pierde la respuesta: ya está en pantalla. Se
                # deja el tiempo de leerla y se sigue escuchando; el estado
                # ERROR era para fallos del turno, no para quedarse mudo.
                self._explain_voice_failure(exc)
                log.warning("sin voz para esta respuesta: %s", exc)
                await self._ipc.broadcast(status("Sin voz ahora mismo: te lo dejo escrito"))
                await self._machine.to(AgentState.SPEAKING)
                await asyncio.sleep(min(10.0, 1.5 + len(spoken) / 15))
                return False

        def early_speak(spoken: str) -> None:
            nonlocal early, early_text
            early_text = spoken
            early = asyncio.create_task(say(spoken), name="tts-early")

        try:
            await self._machine.to(AgentState.THINKING)
            turn = await self._agent.run(text, early_speak=early_speak)
            model_done = time.monotonic() - started
            if turn.cancelled:
                return
            if turn.reply:
                await self._ipc.broadcast(assistant_text(turn.reply, turn.spoken))
                if early is not None and turn.spoken == early_text:
                    await early
                else:
                    if early is not None:
                        # Lo adelantado era un preámbulo («Sure, I'm opening
                        # YouTube…») antes de una herramienta: se deja
                        # terminar y luego va la respuesta final. Cortarlo a
                        # media frase sonaba a fallo.
                        await asyncio.gather(early, return_exceptions=True)
                    await say(turn.spoken)
                log.info("turno: modelo %.1f s · primer audio a %.1f s%s", model_done,
                         (first_audio[0] - started) if first_audio[0] else -1.0,
                         " · voz adelantada" if early is not None else "")
            await self._begin_listening(greet=False)
        except asyncio.CancelledError:
            if early is not None and not early.done():
                early.cancel()
            raise
        except Exception as exc:
            log.exception("fallo en el turno")
            await self._machine.to(AgentState.ERROR)
            await self._ipc.broadcast(error(str(exc)))
            await asyncio.sleep(0.8)
            await self._machine.to(AgentState.IDLE)

    async def _cancel_turn(self) -> None:
        self._agent.cancel()
        await self._tts.stop()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
            try:
                await self._turn_task
            except (asyncio.CancelledError, Exception):
                pass
        self._turn_task = None

    async def _check_follow_up_timeout(self) -> None:
        if self._machine.state is not AgentState.LISTENING or self._capturing:
            return
        if time.monotonic() < self._follow_up_until:
            return
        log.debug("fin de la ventana de seguimiento; vuelvo a escucha pasiva")
        await self._machine.to(AgentState.SLEEPING)
        await self._ipc.broadcast(status(""))
        await asyncio.sleep(0.2)
        await self._machine.to(AgentState.IDLE)

    # ----------------------------------------------------------- confirmaciones
    async def request_confirmation(self, request_id: str, text: str, risk: str) -> bool:
        """Pregunta, y acepta la respuesta por voz o por el botón, lo que llegue antes.

        Mientras suena la pregunta el micrófono sigue vigilando con el mismo
        filtro que el barge-in (volumen y duración), así que un «sí» dicho
        encima corta la pregunta y cuenta. Cuando la pregunta termina, se
        escucha con el VAD normal. Lo que no se entiende como sí o no se ignora
        y se sigue esperando: nunca se convierte en un sí.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_confirms[request_id] = future
        self._confirm_id = request_id
        self._confirm_listening = False
        self._barge_vad.reset()
        self._preroll.clear()
        await self._ipc.broadcast(confirm_request(request_id, text, risk))
        approved = False
        # En pantalla, en el idioma del usuario. En voz, en el idioma de la voz:
        # la primera versión la decía en español con la voz inglesa y salía mal
        # pronunciada y lenta (una pregunta de cinco palabras tardó 12 s entre
        # reintentos). Traducirla cuesta ~1 s y la voz la dice bien.
        spoken = await self._agent.translate_for_speech(text)
        speaking = asyncio.create_task(self._speak(spoken))
        timeout = float(self._config.get("security.confirm_timeout_s", 30))
        try:
            await asyncio.wait({speaking, future}, timeout=timeout,
                               return_when=asyncio.FIRST_COMPLETED)
            if not future.done():
                # La pregunta terminó (o la cortó el usuario): ahora se escucha.
                self._start_confirm_listening()
                await self._ipc.broadcast(status("Di «sí» o «no»"))
                await asyncio.wait_for(future, timeout=timeout)
            approved = bool(future.result())
            return approved
        except asyncio.TimeoutError:
            log.info("confirmación %s expirada: se deniega", request_id)
            approved = False
            return False
        finally:
            await self._ipc.broadcast(confirm_resolved(request_id, approved))
            self._confirm_id = None
            self._confirm_listening = False
            self._capturing = False
            self._utterance.clear()
            self._pending_confirms.pop(request_id, None)
            if not speaking.done():
                speaking.cancel()
            await self._ipc.broadcast(status(""))

    def _start_confirm_listening(self) -> None:
        self._vad.reset()
        self._capturing = False
        self._utterance.clear()
        self._mic.drain()          # fuera el eco de la pregunta
        self._preroll.clear()
        self._confirm_listening = True

    async def _handle_confirm_audio(self, frame: bytes) -> None:
        """Audio mientras hay una confirmación pendiente."""
        if not self._confirm_listening:
            # La pregunta aún suena: sólo cuenta si el usuario habla encima,
            # con el mismo filtro de volumen y duración que el barge-in.
            loud = frame_rms(frame) >= self._barge_min_level
            self._preroll.append(frame)
            if self._barge_vad.push(frame if loud else _SILENT_FRAME) is VadEvent.SPEECH_START:
                preroll = b"".join(self._preroll)
                await self._tts.stop()
                self._start_confirm_listening()
                self._capturing = True
                self._utterance.extend(preroll)
            return

        event = self._vad.push(frame)
        if event is VadEvent.SPEECH_START and not self._capturing:
            self._capturing = True
            self._utterance.clear()
            self._utterance.extend(b"".join(self._preroll))
            self._preroll.clear()
        elif not self._capturing:
            self._preroll.append(frame)
        if self._capturing:
            self._utterance.extend(frame)
            # Una respuesta a «¿sí o no?» no dura cinco segundos. Si se pasa,
            # se cierra y se interpreta lo que haya.
            too_long = len(self._utterance) > 32 * 5000
            if event is VadEvent.SPEECH_END or too_long:
                await self._finish_confirm_utterance()

    async def _finish_confirm_utterance(self) -> None:
        audio = bytes(self._utterance)
        self._capturing = False
        self._utterance.clear()
        self._vad.reset()
        if len(audio) < 4800 or self._confirm_id is None:   # < 150 ms: ruido
            return
        # Sin filtro de alucinaciones: «No, no, no, no.» es un no rotundo, y el
        # filtro de repeticiones lo tiraba (pasó: el usuario tuvo que decir que
        # no tres veces).
        result = await self._stt.transcribe(audio, final=True, strict=False)
        text = result.text.strip()
        log.info("respuesta a la confirmación: %r", text or "(vacía)")
        if not text:
            return
        await self._ipc.broadcast(transcript(text, partial=False))
        decision = consent.interpret(text)
        if decision is None and dismissal.interpret(text) == dismissal.SLEEP:
            # «Descansa» con una pregunta pendiente: no se hace, y a dormir.
            self.resolve_confirmation(self._confirm_id, False)
            await self._dismiss(dismissal.SLEEP, text)
            return
        if decision is None:
            await self._ipc.broadcast(status("No te entendí: di «sí» o «no»"))
            return
        self.resolve_confirmation(self._confirm_id, decision)

    def resolve_confirmation(self, request_id: str, approved: bool) -> None:
        future = self._pending_confirms.get(request_id)
        if future and not future.done():
            future.set_result(approved)

    # ------------------------------------------------------------- comandos UI
    def apply_live_settings(self) -> list[str]:
        """Lo que se puede cambiar sin reiniciar. Devuelve qué se aplicó."""
        applied: list[str] = []
        keyword = str(self._config.get("wake.keyword", "nova"))
        setter = getattr(self._wake, "set_keywords", None)
        if setter is not None:
            setter(keyword)
            applied.append(f"palabra activadora «{keyword}»")
        self._barge_min_level = float(self._config.get("session.barge_in_min_level", 0.04))
        return applied

    async def force_wake(self) -> None:
        if self._machine.state in (AgentState.IDLE, AgentState.SLEEPING):
            await self._machine.to(AgentState.WAKE_WORD_DETECTED)
            await self._begin_listening(greet=True)

    async def interrupt(self) -> None:
        await self._cancel_turn()
        await self._machine.to(AgentState.IDLE)
        await self._ipc.broadcast(status(""))

    def snapshot(self) -> dict[str, Any]:
        return {"state": self._machine.state.value, "capturing": self._capturing}
