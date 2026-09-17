"""Frases que Whisper se inventa cuando el audio no tiene habla clara.

Whisper se entrenó con subtítulos de YouTube, así que cuando le das silencio, un
suspiro o una palabra de medio segundo, rellena con la muletilla más frecuente de
ese corpus: «Muchas gracias y nos vemos en el próximo vídeo», «Subtítulos
realizados por la comunidad de Amara.org», «Suscríbete al canal». No es un error
del audio ni del modelo: es la salida más probable dado un mel vacío.

`vad_filter=True` evita la mayoría porque recorta el silencio antes de decodificar,
pero no todas: una «hola» de 300 ms pasa el VAD y aun así puede disparar la
muletilla. Esta lista es la segunda barrera, y vive aparte del motor para poder
probarla sin tener faster-whisper instalado.
"""
from __future__ import annotations

import re

_PUNCT = str.maketrans({c: " " for c in ",.;:!?¡¿\"'«»()-–—…*"})
_ACCENTS = str.maketrans("áéíóúüñ", "aeiouun")


def normalize(text: str) -> str:
    return " ".join(text.lower().translate(_ACCENTS).translate(_PUNCT).split())


# Fragmentos, no frases exactas: la alucinación varía en los extremos («¡Gracias
# por ver el video!» / «Muchas gracias por ver el vídeo, nos vemos»).
_FRAGMENTS = (
    "nos vemos en el proximo video",
    "gracias por ver el video",
    "gracias por ver este video",
    "hasta el proximo video",
    "suscribete al canal",
    "suscribanse al canal",
    "no olvides suscribirte",
    "dale like y suscribete",
    "subtitulos realizados por la comunidad de amara org",
    "subtitulos por la comunidad de amara org",
    "subtitulado por la comunidad de amara org",
    "mas informacion www alimmenta com",
    "thanks for watching",
    "subscribe to my channel",
    "thank you for watching",
)

# Sonidos que sólo son relleno si son TODO lo que se transcribió.
#
# Esta lista era más larga y estaba mal. Tenía dentro «si», «gracias», «adios»,
# «vamos» y «hasta luego», que son cosas que una persona le dice de verdad a un
# asistente —«sí» es literalmente la respuesta a una confirmación—, y Nova las
# descartaba en silencio. Ahora sólo quedan los sonidos no léxicos y las
# muletillas en inglés, que en una sesión en español no vienen del usuario sino
# del modelo rellenando. El silencio ya lo filtra el VAD antes de decodificar,
# así que esta red no necesita ser tan tupida.
_EXACT = (
    "you",
    "thank you",
    "thanks",
    "amen",
    "ah",
    "eh",
    "em",
    "mm",
    "mmm",
    "hmm",
    "uh",
    "uhm",
)

_REPEATED = re.compile(r"\b(\w+)(?:\s+\1\b){3,}")


def is_hallucination(text: str, *, audio_seconds: float = 0.0) -> bool:
    """¿Esto es relleno del modelo en vez de algo que dijo el usuario?

    `audio_seconds` sirve para el criterio más fiable de todos: si el audio dura
    un segundo y el modelo devuelve veinte palabras, no las oyó. Nadie habla a
    esa velocidad.
    """
    cleaned = normalize(text)
    if not cleaned:
        return True

    for fragment in _FRAGMENTS:
        if fragment in cleaned:
            return True

    if cleaned in _EXACT:
        return True

    # Bucle degenerado: «la la la la la», típico de audio con música o ruido.
    if _REPEATED.search(cleaned):
        return True

    # Más de ~5 palabras por segundo de audio es físicamente imposible en
    # castellano hablado (el habla rápida ronda 3,5).
    if audio_seconds > 0.4:
        words = len(cleaned.split())
        if words > 5 and words / audio_seconds > 5.0:
            return True

    return False
