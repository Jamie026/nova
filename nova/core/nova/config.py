"""Configuración centralizada.

Tres capas, la última gana:
  1. DEFAULTS (este archivo)
  2. settings.json en la carpeta de datos del usuario (lo edita el panel de ajustes)
  3. variables de entorno (.env) — sólo para secretos y overrides puntuales

Se usa un dict con acceso por ruta ("stt.model") en lugar de dataclasses anidadas
porque el panel de ajustes envía parches parciales y así el merge es trivial.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

from .paths import settings_file

DEFAULTS: dict[str, Any] = {
    "language": "es",
    "ipc": {"host": "127.0.0.1", "port": 8765},
    "wake": {
        # La palabra es «Corrin» y Nova no atiende a ninguna otra: si el motor
        # no puede reconocerla, se avisa y queda Ctrl+Shift+Espacio. Nunca se cae
        # a una palabra distinta en silencio.
        # "whisper"      sin clave, reconoce cualquier palabra (el predeterminado)
        # "porcupine"    ~1 % CPU, necesita clave de Picovoice y un .ppn de «Corrin»
        # "openwakeword" sólo si se pide a mano: únicamente sabe "hey jarvis"
        # "off"          sólo Ctrl+Shift+Espacio
        "engine": "whisper",
        "keyword": "corrin",
        # .ppn de «Corrin» generado en console.picovoice.ai (obligatorio con porcupine)
        "keyword_path": "",
        "sensitivity": 0.6,
        # openwakeword: sólo tiene modelos de palabras fijas, ninguno de «Corrin».
        "oww_model": "hey_jarvis",
        "oww_threshold": 0.5,
        # Spotter por Whisper. Es el único de los tres que reconoce «Corrin» sin
        # clave ni modelo entrenado, y por eso importa que sea usable.
        #
        # El valor por defecto era "cpu" por ahorrar batería, y estaba mal: medido
        # aquí, una ventana con voz cuesta tanto en CPU que el spotter llega al
        # 115 % de carga —o sea que no da abasto y se le escapan activaciones—.
        # En GPU la misma ventana se queda en el 25 %. Si prefieres la batería,
        # pon "cpu" a mano y asume que se le escapará alguna.
        "model": "tiny",
        "device": "auto",              # auto|cpu|cuda
        "window_ms": 1200,
        "threshold": 0.85,             # con 0,72 entraban «corrió», «corren» y «cortina»
    },
    "audio": {
        # Índice del micrófono. null = el predeterminado de Windows, que no
        # siempre es el que crees: los portátiles suelen traer el de la webcam
        # además del integrado. `npm run mic:check` lista los índices.
        "input_device": None,
        "output_device": None,
    },
    "vad": {
        "aggressiveness": 2,           # 0-3, webrtcvad
        "start_ms": 150,               # voz continua para considerar "empezó a hablar"
        # Silencio para dar la frase por terminada. Es un compromiso entre dos
        # cosas que el usuario quiere a la vez: que no le corte y que responda
        # rápido. Cada 100 ms aquí son 100 ms de espera con la boca cerrada,
        # pero por debajo de lo que dura una pausa natural, Nova responde a media
        # frase. Con 450 ms, a alguien que habla despacio le cortaba «¿puedes
        # abrir Discord?» en «…abrir Discord» y Whisper oía «Esaurir Discord».
        # 800 ms aguanta pausas para pensar; si hablas rápido y seguido, baja a
        # 500 y ganas tres décimas por frase.
        "end_ms": 800,
        # Cuánto audio anterior al inicio de voz se conserva. El VAD tarda
        # `start_ms` en decidir que alguien está hablando, y sin este colchón esa
        # parte —la primera sílaba— se pierde. 400 ms cubre `start_ms` con margen
        # de sobra y no cuesta nada: son 12 KB de memoria.
        "preroll_ms": 400,
        "max_utterance_ms": 20000,
    },
    "stt": {
        "provider": "faster-whisper",  # | "mock"
        # `medium` y no `small`, porque la precisión con nombres propios es lo
        # que más se nota en uso: `small` oye «disco» donde `medium` oye
        # «Discord», y «Corine» donde «Corin». Cuesta 0,43 s por frase frente a
        # 0,25 (medido en GPU) y 1,2 GB de VRAM en int8. Si vas justo de
        # memoria o prefieres las dos décimas, vuelve a "small".
        # `large-v3` por decisión del usuario: la mejor precisión. Son ~3 GB que
        # baja `npm run models:download` (lo corre setup); si al arrancar no
        # está en disco, Nova empieza con el mayor que haya (ver pick_available)
        # en vez de quedarse sorda descargándolo.
        "model": "large-v3",           # tiny|base|small|medium|large-v3
        # Modelo que se usa si NO hay GPU: en CPU, `small` tarda 1,7 s en
        # transcribir un «hola» (medido), y eso mata la sensación de inmediatez.
        "cpu_model": "tiny",
        "device": "auto",              # auto|cpu|cuda
        "compute_type": "auto",        # auto = int8 en CPU, int8_float16 en GPU
        "beam_size": 5,
        "cpu_threads": 0,              # 0 = lo que decida CTranslate2
        "vad_filter": True,            # recorta silencio: más rápido y sin alucinaciones
        # Pista de vocabulario para el decodificador. Los nombres de aplicaciones
        # son palabras inglesas dichas en español, que es justo donde Whisper
        # falla («Cierra disto» por «Cierra Discord»). Añade aquí las tuyas, y
        # también los nombres de las personas a las que escribes: «Alain Ryan»
        # dicho al micrófono no se parece a nada que Whisper conozca, salvo que
        # se lo digas aquí. Ejemplo: "... Contactos: Alain Ryan, Mamá, Jorge."
        "initial_prompt": (
            "Órdenes para un asistente de escritorio en Windows. Aplicaciones: "
            "Spotify, Discord, Chrome, WhatsApp, YouTube, Steam, Telegram, "
            "Netflix, Word, Excel, Visual Studio Code, Explorador de archivos. "
            "Carpetas: Escritorio, Documentos, Descargas, Imágenes, Vídeos, Música."
        ),
        # Los parciales son sólo texto en pantalla mientras hablas, y cuestan una
        # decodificación entera cada 900 ms que compite con la transcripción
        # final por el mismo modelo. Con la latencia como prioridad, apagados.
        "partials": False,
        "partial_interval_ms": 900,
    },
    "llm": {
        "provider": "deepseek",        # | "mock"
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "temperature": 0.3,
        "max_steps": 8,                # tope de pasos del planner (anti-loop)
        # Las respuestas se leen en voz alta: dos frases. El tope evita pagar
        # (en segundos, no en dinero) un párrafo que además nadie quiere oír.
        "max_tokens": 300,
        "request_timeout_s": 60,
    },
    "tts": {
        # "auto"        chatterbox si está instalado (setup.ps1 lo instala cuando
        #               hay GPU NVIDIA); si no, edge. Es el valor por defecto para
        #               que quien clone el repo tenga la voz sin tocar nada.
        # "edge"        voces neuronales de Microsoft; necesita red, ~1,3 s por respuesta
        # "chatterbox"  voz clonada en local sobre la GPU (Chatterbox Turbo, MIT)
        #               a partir de la referencia (ver app.find_reference). Sólo
        #               inglés: speak_language pasa a "en" solo.
        # "mock"        sin audio
        "provider": "auto",
        "voice": "es-PE-CamilaNeural",
        "rate": "+8%",
        "volume": "+0%",
        # Idioma de lo que se DICE, cuando debe ser distinto del de la pantalla.
        # Vacío = "en" con la voz clonada y el mismo que `language` con las
        # demás. Con "en", el modelo devuelve las dos versiones en una sola
        # respuesta: se oye inglés y se lee español.
        "speak_language": "",
        # --- sólo para "chatterbox" ---
        # WAV de referencia del que se copia el timbre. Vacío = la del usuario
        # en %APPDATA%/Nova/voices/reference.wav (la deja `npm run voice:build
        # <carpeta con audios>`) y, si no existe, la incluida en el repo
        # (nova/tts/voices/reference.wav). El modelo usa los primeros 10 s
        # para el timbre fino: que empiece por lo mejor.
        "reference": "",
        "device": "auto",              # auto|cpu|cuda — en CPU es inviable
    },
    "session": {
        "greeting": "Te escucho.",
        "speak_greeting": False,       # earcon por defecto; la voz añade ~600 ms
        # Una vez despierta, Nova NO se duerme por inactividad: sólo con «descansa»
        # o con el botón de interrumpir (lo pidió el usuario). Con True vuelve a
        # dormirse sola tras follow_up_seconds sin que nadie le hable.
        "sleep_on_idle": False,
        "follow_up_seconds": 12,       # conversación continua sin repetir wake word
        "dictation_pause_ms": 2500,    # silencio que da por terminado un dictado letra a letra
        # Lo que dice al oír «descansa» (o «eso es todo», «hasta luego»): se
        # calla, dice esto y se duerme sin pasar por el modelo. En el idioma
        # de la voz; vacío = se duerme sin decir nada.
        "farewell": "Okay, resting.",
        "barge_in": True,
        # Interrumpir a Nova mientras habla exige más que la escucha normal, porque
        # el micrófono oye los altavoces y Nova se interrumpía a sí misma. Voz
        # seguida mínima (ms) y volumen mínimo (RMS lineal, 0..1). Si usas
        # auriculares no hay eco y puedes bajar el nivel a 0.01; si tu micrófono
        # sigue oyendo a Nova, súbelo, o pon barge_in en false.
        "barge_in_start_ms": 400,
        "barge_in_min_level": 0.04,
    },
    "security": {
        # auto | confirm por nivel de riesgo. Lo que se manda a otra persona
        # (send_message, send_file_to_app con send=true) pregunta SIEMPRE, se
        # ponga lo que se ponga aquí: no se deshace y afecta a alguien más.
        # Abrir apps, archivos y carpetas no pregunta, salvo un archivo que se
        # ejecutaría al abrirlo (.exe, .bat, .vbs...).
        "low": "auto",
        "medium": "confirm",
        "high": "confirm",
        "confirm_timeout_s": 30,
        # Herramientas que no piden permiso aunque sean de riesgo medio. Para
        # que Nova navegue por la pantalla sin interrumpirte a cada paso:
        #   ["read_screen", "click_item", "open_chat"]
        # Las de riesgo alto (send_message, type_text, click_at...) no entran
        # aquí ni aunque las pongas: enviar un mensaje no se deshace.
        # process_invoices envía sin preguntar lo que es seguro y aparta lo dudoso:
        # lo pidió el usuario («sólo pregunta si no está seguro del monto»).
        "auto_approve": ["process_invoices"],
        "tool_allowlist": [],          # vacío = todas las herramientas registradas
        "app_allowlist": [],           # vacío = cualquier app instalada
    },
    # Envío de WhatsApp en lote (ver messaging_tools.BATCH_DEFAULTS).
    "messaging": {
        "country_code": "51",          # Perú: 987654321, +51…, 0051… y 51… valen igual
        "batch_size": 20,              # mensajes por tanda
        "batch_pause_min": 15,         # pausa entre tandas
        # Espera entre mensajes, en segundos (lo pidió el usuario): 5 s, y 10 s si
        # el envío pasa de large_batch mensajes.
        "gap_s": [5, 5],
        "gap_large_s": [10, 10],
        "large_batch": 20,
        "daily_limit": 150,            # tope en 24 h; el resto sigue al día siguiente
        "warn_s": 10,                  # aviso antes de cada tanda (Nova usa ratón y teclado)
    },
    "browser": {
        # Cómo se controla el navegador:
        #   "auto"      el que ya tengas abierto (Opera, Chrome, Edge, Firefox...)
        #               por teclado: abre en TU pestaña, con TUS sesiones. Si no
        #               hay ninguno abierto, lanza Chrome por CDP.
        #   "keyboard"  siempre el que tengas abierto; si no hay, falla.
        #   "cdp"       siempre el Chrome propio de Nova (perfil aparte). Es el
        #               único que puede leer la página o pulsar por nombre sin
        #               pasar por la pantalla.
        "mode": "auto",
        "prefer": "",                  # p. ej. "opera" si tienes varios abiertos
        "channel": "chrome",           # chrome | msedge (para el modo cdp)
        "cdp_port": 9222,
        "attach_timeout_s": 20,
        "default_search": "https://www.google.com/search?q={query}",
    },
    "files": {
        # Vacío = carpetas del usuario detectadas por el adaptador del SO
        # (Escritorio, Documentos, Descargas, Imágenes, Vídeos, Música y OneDrive).
        # Lo que no cuelgue de estas raíces no existe para el agente.
        "search_roots": [],
        "max_results": 12,
        "max_depth": 6,
        "budget_s": 6.0,
    },
    "screen": {"output_dir": ""},      # vacío = Imágenes/Nova
    "control": {
        # Inyección de teclado y ratón. Con false, esas herramientas ni siquiera
        # se registran: el modelo no las ve y no puede pedirlas.
        "enabled": True,
    },
    "memory": {"enabled": True, "max_turns": 24, "max_actions": 20},
    # skin: "portrait" (el retrato del personaje) | "aperture" (el skin procedural)
    "avatar": {"skin": "portrait", "scale": 1.0},
    "dev": {"log_level": "INFO", "mock": False},
}

_ENV_MAP = {
    "NOVA_IPC_PORT": ("ipc.port", int),
    "NOVA_WAKE_ENGINE": ("wake.engine", str),
    "NOVA_WAKE_KEYWORD_PATH": ("wake.keyword_path", str),
    "NOVA_STT_MODEL": ("stt.model", str),
    "NOVA_STT_DEVICE": ("stt.device", str),
    "NOVA_LLM_MODEL": ("llm.model", str),
    "NOVA_LLM_BASE_URL": ("llm.base_url", str),
    "NOVA_TTS_VOICE": ("tts.voice", str),
    "NOVA_TTS_PROVIDER": ("tts.provider", str),
    "NOVA_TTS_SPEAK_LANG": ("tts.speak_language", str),
    "NOVA_TTS_REFERENCE": ("tts.reference", str),
    "NOVA_LOG_LEVEL": ("dev.log_level", str),
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _deep_diff(base: dict, current: dict) -> dict:
    """Sólo lo que `current` cambia respecto a `base`, con la misma forma."""
    out: dict = {}
    for key, value in current.items():
        if key not in base:
            out[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(base[key], dict):
            sub = _deep_diff(base[key], value)
            if sub:
                out[key] = sub
        elif value != base[key]:
            out[key] = copy.deepcopy(value)
    return out


def _prune_empty(node: dict) -> dict:
    out = {}
    for key, value in node.items():
        if isinstance(value, dict):
            value = _prune_empty(value)
            if not value:
                continue
        out[key] = value
    return out


def _unset(node: dict, dotted: str) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.get(part)
        if not isinstance(node, dict):
            return
    node.pop(parts[-1], None)


class Config:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    @classmethod
    def load(cls) -> "Config":
        data = copy.deepcopy(DEFAULTS)
        path = settings_file()
        if path.exists():
            try:
                data = _deep_merge(data, json.loads(path.read_text("utf-8")))
            except (OSError, json.JSONDecodeError):
                pass  # settings corrupto no debe impedir arrancar
        cfg = cls(data)
        for env_key, (dotted, caster) in _ENV_MAP.items():
            raw = os.environ.get(env_key)
            if raw:
                cfg.set(dotted, caster(raw))
        if os.environ.get("NOVA_MOCK", "").lower() in ("1", "true", "yes"):
            cfg.set("dev.mock", True)
        # Freno de mano para desarrollo y demos: desactiva teclado y ratón
        # simulados sin tocar settings.json.
        if os.environ.get("NOVA_NO_INPUT", "").lower() in ("1", "true", "yes"):
            cfg.set("control.enabled", False)
        return cfg

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def patch(self, patch: dict[str, Any]) -> None:
        self._data = _deep_merge(self._data, patch)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def save(self) -> None:
        """Guarda sólo lo que el usuario ha cambiado, no una foto de todo.

        La versión anterior volcaba la configuración entera. Parecía inocuo y no
        lo era: al pulsar «Guardar» en el panel, cada valor por defecto quedaba
        congelado en settings.json, y como ese archivo pisa a DEFAULTS, cualquier
        mejora posterior de un valor por defecto se ignoraba en silencio para
        siempre. Pasó de verdad: `vad.end_ms` subió de 450 a 800 en el código y
        el usuario seguía con 450 sin saberlo. Guardar sólo la diferencia deja
        que los defaults evolucionen y que lo del usuario mande sólo donde él
        tocó algo.

        Tampoco se guarda lo que viene del entorno: un override del .env es
        temporal por definición, y persistirlo lo convertiría en permanente
        aunque después se borre la línea.
        """
        patch = _deep_diff(DEFAULTS, self._data)
        for env_key, (dotted, _caster) in _ENV_MAP.items():
            if os.environ.get(env_key):
                _unset(patch, dotted)
        if os.environ.get("NOVA_MOCK", "").lower() in ("1", "true", "yes"):
            _unset(patch, "dev.mock")
        if os.environ.get("NOVA_NO_INPUT", "").lower() in ("1", "true", "yes"):
            _unset(patch, "control.enabled")
        patch = _prune_empty(patch)   # {"tts": {}} no aporta nada y confunde
        settings_file().write_text(
            json.dumps(patch, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @property
    def mock(self) -> bool:
        return bool(self.get("dev.mock"))
