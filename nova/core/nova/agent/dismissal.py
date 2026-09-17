"""Reconocer un «descansa» o un «para» dicho a Nova.

Hay dos órdenes que no deben pasar por el modelo: se contestan al instante y
sin conversación.

  * Despedida («descansa», «eso es todo», «hasta luego»): Nova se calla si
    estaba hablando, se despide con dos palabras y se va a dormir. Antes iba
    al modelo, que contestaba con una frase de cinco segundos («Thanks, I'll
    be right here whenever you need me — enjoy your break…») y seguía
    escuchando doce segundos más.
  * Parar («para», «detente», «cancela»): corta lo que esté haciendo o
    diciendo y vuelve a escuchar, por si viene otra orden.

Igual que en consent.py, se es conservador: las frases sólo cuentan si la
orden es corta (una despedida no tiene diez palabras) y algunas sólo si son
toda la frase. «Para» es preposición en «para abrir YouTube», y «nada más» es
«sólo» en «nada más abre Spotify»: esas van en la lista de frases exactas.
"""
from __future__ import annotations

from .consent import _normalize

SLEEP = "sleep"
STOP = "stop"

# Frases que valen en cualquier punto de una orden corta.
_SLEEP_ANYWHERE = (
    "descansa", "descansar", "descansas", "duermete", "vete a dormir", "a dormir",
    "hasta luego", "hasta manana", "hasta mañana", "adios", "chao", "chau",
    "buenas noches", "eso es todo", "es todo por hoy", "hemos terminado",
    "go to sleep", "good night", "goodbye", "bye bye", "sleep now",
)
_STOP_ANYWHERE = (
    "detente", "parate", "cancela", "cancelalo", "cancela eso", "callate",
    "silencio", "basta", "olvidalo", "dejalo", "stop", "cancel", "shut up",
)
# Frases que sólo cuentan si son toda la orden (quitados el nombre y el relleno).
_SLEEP_EXACT = ("nada mas", "es todo", "ya esta", "terminamos", "rest", "bye")
_STOP_EXACT = ("para", "para ya", "para para", "alto", "espera", "quieto", "no", "no no", "no no no")

# Lo que sobra alrededor: el nombre, la cortesía, el «ya».
_FILLERS = (
    "corrin", "corin", "nova", "oye", "hey", "ey", "por favor", "porfa", "ya", "ahora",
    "gracias", "muchas gracias", "buen trabajo", "bien hecho", "bien", "ok", "okey", "vale",
    "bueno", "listo", "puedes", "ya puedes", "y",
)
_MAX_WORDS = 8


def _strip_fillers(cleaned: str) -> str:
    words = cleaned.split()
    changed = True
    while words and changed:
        changed = False
        for filler in sorted(_FILLERS, key=len, reverse=True):
            parts = filler.split()
            n = len(parts)
            if words[:n] == parts:
                words = words[n:]
                changed = True
            if words[-n:] == parts and len(words) >= n:
                words = words[:-n]
                changed = True
    return " ".join(words)


def _contains(cleaned: str, phrases: tuple[str, ...]) -> bool:
    padded = f" {cleaned} "
    return any(f" {phrase} " in padded for phrase in phrases)


def interpret(text: str) -> str | None:
    """SLEEP (despedida), STOP (parar) o None (no es ninguna de las dos)."""
    cleaned = _normalize(text)
    if not cleaned or len(cleaned.split()) > _MAX_WORDS:
        return None
    core = _strip_fillers(cleaned)
    if _contains(cleaned, _SLEEP_ANYWHERE) or core in _SLEEP_EXACT:
        return SLEEP
    if _contains(cleaned, _STOP_ANYWHERE) or core in _STOP_EXACT:
        return STOP
    return None
