"""Separa lo que Nova *dice* de lo que Nova *muestra*.

La voz clonada es inglesa y suena bastante mejor hablando inglés que español: con
la misma referencia, midiendo doce frases contra su transcripción, el inglés da
0,90 de fidelidad y un fallo, y el español 0,79 y siete. La diferencia no es de
ajuste, es de pedirle a un modelo de clonado que cambie de idioma.

Así que el modelo devuelve las dos versiones en una sola respuesta —dos etiquetas
en líneas separadas— y aquí se parten: el inglés va al sintetizador y el español a
los subtítulos. Dos versiones en una llamada y no dos llamadas, porque una segunda
llamada al modelo para traducir costaría más de un segundo en el sitio exacto
donde el usuario está esperando.

Vive aparte del orquestador porque es texto puro y conviene poder probar cada caso
raro sin levantar nada.
"""
from __future__ import annotations

from .tone import naturalize

import re

# Se acepta con y sin negritas, con dos puntos o guion, y en cualquier orden. Un
# modelo obedece el formato casi siempre, pero "casi" no es "siempre", y el coste
# de ser permisivo aquí es una expresión regular algo más fea.
# Los marcadores de markdown pueden caer a cualquier lado de los dos puntos: el
# modelo escribe tanto «**EN:** hola» como «**EN**: hola».
_TAG = re.compile(
    r"^\s*[*_`]{0,2}\s*(EN|ES|VOZ|TEXTO)\s*[*_`]{0,2}\s*[:\-–]\s*[*_`]{0,2}\s*(.*?)\s*$",
    re.IGNORECASE,
)
_SPOKEN_TAGS = {"en", "voz"}
_SUBTITLE_TAGS = {"es", "texto"}


def split_bilingual(reply: str) -> tuple[str, str]:
    """Devuelve (lo_que_se_dice, lo_que_se_muestra).

    Si no encuentra las dos etiquetas devuelve el texto tal cual en ambos: más
    vale que Nova diga y muestre lo mismo a que se quede muda porque el modelo se
    saltó un formato.
    """
    spoken: list[str] = []
    subtitle: list[str] = []
    current: list[str] | None = None

    for line in reply.splitlines():
        match = _TAG.match(line)
        if match:
            tag = match.group(1).lower()
            current = spoken if tag in _SPOKEN_TAGS else subtitle if tag in _SUBTITLE_TAGS else None
            if current is not None:
                rest = match.group(2).strip()
                if rest:
                    current.append(rest)
            continue
        if current is not None and line.strip():
            current.append(line.strip())

    hablado = naturalize(" ".join(spoken).strip())
    mostrado = naturalize(" ".join(subtitle).strip())

    if hablado and mostrado:
        return hablado, mostrado
    # Con una sola etiqueta, esa es la respuesta entera: el modelo no dio la otra
    # versión, así que se usa la misma para las dos cosas.
    unica = (hablado or mostrado or naturalize(reply)).strip()
    return unica, unica


def spoken_so_far(partial: str) -> str | None:
    """La parte hablada de una respuesta que todavía está llegando, o None.

    El modelo escribe primero «EN: …» y luego «ES: …». En cuanto aparece la
    etiqueta del subtítulo, la línea inglesa está completa y la voz puede
    arrancar sin esperar a la traducción, que es la mitad del tiempo de
    generación. Sólo cuenta si hay texto hablado y la etiqueta de subtítulo
    llegó DESPUÉS de él; con el orden invertido se espera al final.
    """
    spoken: list[str] = []
    current: str | None = None
    for line in partial.splitlines():
        match = _TAG.match(line)
        if match:
            tag = match.group(1).lower()
            if tag in _SUBTITLE_TAGS:
                text = " ".join(spoken).strip()
                return text or None
            current = "spoken" if tag in _SPOKEN_TAGS else None
            rest = match.group(2).strip()
            if current and rest:
                spoken.append(rest)
            continue
        if current and line.strip():
            spoken.append(line.strip())
    return None
