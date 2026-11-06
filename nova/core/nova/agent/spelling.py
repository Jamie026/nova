"""Convertir lo dictado letra por letra en el texto que el usuario quería decir.

Whisper no devuelve «juan@gmail.com»: devuelve «jota u a ene arroba gmail punto
com», «J. U. A. N.» o «J-U-A-N», según el día. Aquí se reconocen los nombres de
las letras en español, con lo que se dice de verdad en Latinoamérica («be larga»,
«ve chica», «doble u») y lo que Whisper escribe cuando oye seseo («se» por «ce»).
Las palabras que no son una letra («gmail», «com») se conservan tal cual.
"""
from __future__ import annotations

import re

# La pista para Whisper mientras se dicta: sin ella escribe palabras donde se
# dijeron letras («ele» → «él», «ge» → «je»).
SPELLING_PROMPT = (
    "Dictado letra por letra: jota, u, a, ene, arroba, gmail, punto, com. "
    "A, be, ce, de, e, efe, ge, hache, i, jota, ka, ele, eme, ene, eñe, o, pe, cu, "
    "erre, ese, te, u, uve, doble ve, equis, ye, zeta, guion."
)

_LETTERS = {
    "a": "a", "be": "b", "b": "b", "ce": "c", "se": "c", "c": "c", "de": "d", "d": "d",
    "e": "e", "efe": "f", "f": "f", "ge": "g", "je": "g", "g": "g", "hache": "h",
    "ache": "h", "h": "h", "i": "i", "jota": "j", "j": "j", "ka": "k", "ca": "k", "k": "k",
    "ele": "l", "l": "l", "eme": "m", "m": "m", "ene": "n", "n": "n", "eñe": "ñ", "ñ": "ñ",
    "o": "o", "pe": "p", "p": "p", "cu": "q", "que": "q", "q": "q", "erre": "r", "ere": "r",
    "r": "r", "ese": "s", "s": "s", "te": "t", "t": "t", "u": "u", "uve": "v", "ube": "v",
    "ve": "v", "v": "v", "w": "w", "equis": "x", "ekis": "x", "x": "x", "ye": "y", "y": "y",
    "zeta": "z", "ceta": "z", "seta": "z", "z": "z",
}
_DIGITS = {"cero": "0", "uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
           "seis": "6", "siete": "7", "ocho": "8", "nueve": "9"}
_SYMBOLS = {"arroba": "@", "punto": ".", "guion": "-", "espacio": " "}
_PAIRS = {
    ("doble", "ve"): "w", ("doble", "u"): "w", ("doble", "uve"): "w", ("uve", "doble"): "w",
    ("i", "griega"): "y", ("guion", "bajo"): "_", ("barra", "baja"): "_",
    ("be", "larga"): "b", ("be", "grande"): "b", ("be", "alta"): "b",
    ("ve", "corta"): "v", ("ve", "chica"): "v", ("ve", "baja"): "v", ("ve", "pequena"): "v",
}
_FILLER = {"mayuscula", "mayusculas", "minuscula", "minusculas", "letra", "letras", "mas",
           "luego", "despues", "es", "soy"}
_LEADING_ONLY = {"es", "soy"}
_CANCEL = {"cancela", "cancelar", "olvidalo", "dejalo", "dejalo asi"}
_ADDRESS = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+")


def _tokens(text: str) -> list[str]:
    table = str.maketrans("áéíóúüÁÉÍÓÚÜ", "aeiouuaeiouu")
    clean = text.translate(table).lower()
    # Los guiones separan letras («J-U-A-N»); uno de verdad se dice «guion».
    clean = re.sub(r"[,;:!?¿¡\"'()«»\-]", " ", clean)
    # Un punto al final de un trozo es el de Whisper («J. U. A.»), no el dictado.
    return [token.strip(".") for token in clean.split() if token.strip(".")]


def is_cancel(text: str) -> bool:
    return " ".join(_tokens(text)) in _CANCEL


def is_email(text: str) -> bool:
    return bool(_ADDRESS.fullmatch(text))


def decode(text: str, kind: str = "name") -> str:
    """`kind`: "email" (minúsculas y sin espacios) o "name" (con iniciales)."""
    tokens = _tokens(text)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        following = tokens[i + 1] if i + 1 < len(tokens) else ""
        if (token, following) in _PAIRS:
            out.append(_PAIRS[(token, following)])
            i += 2
            continue
        i += 1
        # «es» y «soy» sólo son relleno al principio («es jota u a ene...»):
        # después pueden ser parte del dato («hotmail punto es»).
        if token in _FILLER and (token not in _LEADING_ONLY or not out):
            continue
        if token in ("be", "ve") and following == "de" and i + 1 < len(tokens):
            i += 2                  # «be de burro»: la palabra sólo aclara la letra
        out.append(_SYMBOLS.get(token) or _LETTERS.get(token) or _DIGITS.get(token) or token)
    joined = "".join(out)
    if kind == "email":
        return joined.replace(" ", "").lower()
    return " ".join(word.capitalize() for word in joined.split())


def read_back(value: str) -> str:
    """Cómo se le repite al usuario, para que pueda comprobarlo letra por letra."""
    if "@" not in value:
        return ", ".join(c if c != " " else "espacio" for c in value)
    local, domain = value.split("@", 1)
    return ", ".join([*local, "arroba", domain.replace(".", " punto ")])
