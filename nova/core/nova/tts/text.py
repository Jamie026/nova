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
            cut = chunk.rfind(" ", 0, max_chars)
            cut = cut if cut > 40 else max_chars
            parts.append(chunk[:cut].strip())
            chunk = chunk[cut:].strip()
        if chunk:
            parts.append(chunk)
    return parts


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


def plan_chunks_streaming(text: str, first_max: int = 40, growth: float = 1.7,
                          rest_max: int = 180) -> list[str]:
    """Reparto para un sintetizador local, donde el coste crece con el texto.

    Con edge-tts el tamaño del trozo apenas importa: casi todo el tiempo es abrir
    la conexión, así que compensa hacer trozos grandes y pocos. Con XTTS es al
    revés: no hay conexión, y sintetizar tarda algo más de la mitad de lo que
    dura el audio. Medido: 23 caracteres tardan 1,9 s en sonar; 131 tardan 5,3.
    Un primer trozo largo es exactamente lo que hace que Nova «tarde en
    responder».

    Así que el primer trozo es minúsculo —la primera cláusula, «Hi!» o «Listo,»—
    para que suene casi al instante, y cada trozo siguiente puede ser hasta
    `growth` veces más largo que el anterior. Ese factor no es estético: mientras
    suena el trozo N se sintetiza el N+1, y con un coste de ~0,6 s por segundo de
    audio, el siguiente cabe sin hueco si no pasa de ~1,6 veces el anterior. Con
    1,7 se medían huecos de 0,3-0,7 s entre los trozos largos; con 1,5 el primer
    audio llega igual de rápido y el resto encadena sin costura.

    Se corta también por comas y no sólo por puntos, porque una frase de 130
    caracteres sin un punto dentro es de lo más normal en una respuesta.
    """
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(text.strip()) if c.strip()]
    if not clauses:
        return []

    def next_limit(done: str) -> int:
        return min(rest_max, max(first_max, int(len(done) * growth)))

    chunks: list[str] = []
    current = ""
    limit = first_max
    for clause in clauses:
        while clause:
            candidate = f"{current} {clause}".strip() if current else clause
            if len(candidate) <= limit:
                current, clause = candidate, ""
            elif current:
                # No cabe junto a lo acumulado: se cierra el trozo y se reintenta
                # la cláusula sola con el límite siguiente.
                chunks.append(current)
                limit, current = next_limit(current), ""
            else:
                # La cláusula sola supera el límite: se parte por el último
                # espacio que quepa. Mejor una costura dentro de una frase
                # larguísima que esperar cinco segundos a que se sintetice entera.
                cut = clause.rfind(" ", 0, limit)
                cut = cut if cut > 20 else limit
                chunks.append(clause[:cut].strip())
                limit, clause = next_limit(chunks[-1]), clause[cut:].strip()
    if current:
        chunks.append(current)
    return chunks


def plan_whole(text: str, max_chars: int = 230) -> list[str]:
    """Todo junto, partido por frases sólo cuando no cabe en una llamada.

    Es el reparto que conserva la voz: XTTS fija el timbre con el contexto de
    la frase entera, y cada corte es una ocasión para que se le vaya. Sólo se
    corta cuando el texto pasa del límite del modelo, y siempre por frases.
    """
    chunks: list[str] = []
    current = ""
    for sentence in split_sentences(text, max_chars=max_chars):
        candidate = f"{current} {sentence}".strip() if current else sentence
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
