"""Troceado de texto para síntesis.

Vive aparte del motor porque es lógica pura: así se puede probar en CI sin tener
tarjeta de sonido ni PortAudio instalado.
"""
from __future__ import annotations

import re

_SENTENCE_SPLIT = re.compile(r"(?<=[\.\!\?\:;…])\s+|\n+")


def split_sentences(text: str, max_chars: int = 220) -> list[str]:
    """Parte en frases y corta las frases muy largas por el último espacio."""
    parts: list[str] = []
    for chunk in _SENTENCE_SPLIT.split(text.strip()):
        chunk = chunk.strip()
        while len(chunk) > max_chars:
            # En partes parecidas, no «todo lo que quepa y un resto»: un
            # resto de tres palabras es justo lo que peor sintetiza el
            # modelo. Se corta por la coma más cercana al medio, o por un
            # espacio.
            n = -(-len(chunk) // max_chars)
            cut = _best_cut(chunk, len(chunk) // n, max_chars)
            parts.append(chunk[:cut].strip())
            chunk = chunk[cut:].strip()
        if chunk:
            parts.append(chunk)
    return parts


def _best_cut(chunk: str, target: int, max_chars: int) -> int:
    """Índice de corte cerca de `target`: la coma más cercana dentro de ±40 %,
    si no el espacio más cercano, si no el último espacio antes del máximo."""
    lo, hi = max(1, int(target * 0.6)), min(len(chunk) - 1, int(target * 1.4), max_chars)
    commas = [i + 1 for i in range(lo, hi) if chunk[i] in ",;"]
    if commas:
        return min(commas, key=lambda i: abs(i - target))
    spaces = [i for i in range(lo, hi) if chunk[i] == " "]
    if spaces:
        return min(spaces, key=lambda i: abs(i - target))
    cut = chunk.rfind(" ", 0, max_chars)
    return cut if cut > 40 else max_chars



def plan_chunks(text: str, first_max: int = 110, rest_max: int = 320) -> list[str]:
    """Agrupa las frases en los pocos trozos que se van a sintetizar.

    Trocear frase a frase sonaba bien sobre el papel —la primera sílaba llega
    antes— pero en la práctica se oía a trompicones: cada trozo es una petición
    nueva a edge-tts, o sea un websocket nuevo contra Microsoft, y eso son 300-600
    ms de silencio *entre* frases. Una respuesta de dos frases tardaba nueve
    segundos en decirse.

    El reparto que sí funciona: la primera frase sola, para que la voz empiece
    pronto, y todo lo demás junto, porque mientras suena la primera ya se está
    sintetizando el resto y el usuario nunca llega a oír la costura. Sólo se
    vuelve a partir si el resto es tan largo que no cabe en una petición.
    """
    sentences = split_sentences(text, max_chars=rest_max)
    if not sentences:
        return []

    head = sentences[0]
    rest = sentences[1:]
    # Una primera frase muy corta («Listo.») no compensa: se agrupa con la
    # siguiente para no pagar dos conexiones por medio segundo de audio.
    while rest and len(head) < 25:
        head = f"{head} {rest.pop(0)}"
    if len(head) > first_max and not rest:
        return [head]

    chunks = [head]
    current = ""
    for sentence in rest:
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= rest_max:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


_CLAUSE_SPLIT = re.compile(r"(?<=[\.\!\?\:;…,—–])\s+|\n+")


def plan_pieces(text: str, max_chars: int = 140, min_chars: int = 40,
                first_max: int | None = None, growth: float = 0.0) -> list[str]:
    """Trozos por oraciones para sintetizar cada uno de una pasada.

    `first_max` acota la primera pieza (es lo que se espera entero antes de
    oír nada) y `growth` deja que cada pieza sea como mucho ese factor más
    larga que la anterior: con la voz a rtf ~0,6, una pieza se genera en
    ~0,6 veces su duración más 0,3 s, así que si la siguiente no crece más
    de 1,5x, siempre está lista antes de que termine la anterior y la
    tubería no espera. Nunca cierra un trozo de menos de `min_chars`: las
    frases muy cortas son las que peor salen, así que se le pega la
    siguiente oración aunque se pase del límite."""
    sentences = split_sentences(text, max_chars=first_max or max_chars)
    pieces: list[str] = []
    current = ""
    limit = first_max or max_chars
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if current and len(candidate) > limit and len(current) >= min_chars:
            pieces.append(current)
            current = sentence
            limit = min(max_chars, int(len(pieces[-1]) * growth)) if growth else max_chars
            limit = max(limit, min_chars)
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces
