"""Las dos reglas que impiden que Nova conteste a algo que nadie dijo, y el
reparto de trozos que hace que la respuesta suene seguida."""
import pytest

from nova.stt.hallucinations import is_hallucination
from nova.tts.text import plan_chunks


@pytest.mark.parametrize("texto", [
    "¡Muchas gracias y nos vemos en el próximo vídeo!",
    "Gracias por ver el video, suscríbete al canal",
    "Subtítulos realizados por la comunidad de Amara.org",
    "Thanks for watching!",
])
def test_descarta_las_muletillas_de_subtitulos(texto):
    # Es exactamente lo que respondió Whisper a un «hola» de un segundo.
    assert is_hallucination(texto, audio_seconds=1.07)


@pytest.mark.parametrize("texto", [
    "hola",
    "abre spotify",
    "gracias, ahora cierra el navegador",
    "busca la imagen corin y mándala por discord",
])
def test_no_descarta_ordenes_de_verdad(texto):
    assert not is_hallucination(texto, audio_seconds=2.5)


def test_descarta_lo_que_no_cabe_en_el_audio():
    # Veinte palabras en un segundo: nadie habla así.
    largo = "abre el navegador y busca vuelos baratos a cusco para el mes que viene por favor ahora"
    assert is_hallucination(largo, audio_seconds=1.0)
    assert not is_hallucination(largo, audio_seconds=6.0)


def test_descarta_bucles_degenerados():
    assert is_hallucination("la la la la la", audio_seconds=3.0)


def test_una_respuesta_corta_es_una_sola_peticion():
    # El caso que sonaba a trompicones: dos frases, dos conexiones, un silencio
    # de medio segundo en medio.
    chunks = plan_chunks("¡Gracias a ti! Nos vemos luego.")
    assert len(chunks) == 1


def test_la_primera_frase_sale_sola_si_ya_es_bastante_larga():
    texto = ("Listo, abrí Spotify y puse la lista que escuchaste ayer. "
             "También bajé el volumen al treinta por ciento porque lo tenías al tope. "
             "Dime si quieres otra cosa.")
    chunks = plan_chunks(texto)
    assert len(chunks) == 2
    assert chunks[0].startswith("Listo")
    assert "".join(chunks).replace(" ", "") == texto.replace(" ", "")


def test_no_pierde_texto_al_agrupar():
    texto = " ".join(f"Frase número {i}." for i in range(40))
    assert " ".join(plan_chunks(texto)) == texto


@pytest.mark.parametrize("texto", ["sí", "gracias", "adiós", "vamos", "hasta luego", "listo"])
def test_no_descarta_palabras_sueltas_que_si_se_dicen(texto):
    # Estas estaban en la lista de descarte y no debían estar: «sí» es la
    # respuesta a una confirmación, y Nova la tiraba sin decir nada.
    assert not is_hallucination(texto, audio_seconds=0.8)


# --- reparto para sintetizador local -----------------------------------------
from nova.tts.text import plan_chunks_streaming


def test_streaming_el_primer_trozo_es_minusculo():
    texto = ("Hi! I can open and close apps, search the web, manage files, take "
             "screenshots, control volume and media, and type or click for you. "
             "What do you need?")
    chunks = plan_chunks_streaming(texto, first_max=40, growth=1.7, rest_max=180)
    assert len(chunks[0]) <= 40
    assert chunks[0].startswith("Hi!")
    # Cada trozo cabe en el tiempo que suena el anterior: no crece más de 1,7x.
    for antes, despues in zip(chunks, chunks[1:]):
        assert len(despues) <= max(40, int(len(antes) * 1.7)) + 0
    assert " ".join(chunks) == texto


def test_streaming_no_pierde_texto_ni_con_frases_sin_puntuacion():
    texto = "palabra " * 60
    chunks = plan_chunks_streaming(texto.strip(), first_max=40, rest_max=100)
    assert " ".join(chunks) == texto.strip()
    assert all(len(c) <= 100 for c in chunks)
    assert len(chunks[0]) <= 40


def test_streaming_una_frase_corta_queda_entera():
    assert plan_chunks_streaming("Done, I opened Discord.") == ["Done, I opened Discord."]


def test_modo_entero_no_parte_una_respuesta_normal():
    from nova.tts.xtts_engine import XttsEngine
    motor = XttsEngine(reference="x.wav", language="en", chunking="whole")
    texto = "Done, I opened the browser with a new tab ready to search. Just tell me what to look for."
    assert motor._plan(texto) == [texto]
    # Sólo se parte cuando pasa del límite del modelo, y por frases.
    largo = " ".join(f"Sentence number {i} is here." for i in range(20))
    trozos = motor._plan(largo)
    assert len(trozos) > 1 and all(len(t) <= 230 for t in trozos)
    assert " ".join(trozos) == largo
