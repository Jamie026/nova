import pytest

from nova.tts.text import split_sentences
from nova.tts.mock_tts import MockTts


def test_parte_por_frases_para_reducir_latencia():
    partes = split_sentences("Listo. Abrí Spotify y puse música. ¿Quieres otra cosa?")
    assert len(partes) == 3 and partes[0] == "Listo."


def test_parte_frases_muy_largas():
    largo = "palabra " * 120
    partes = split_sentences(largo, max_chars=200)
    assert all(len(p) <= 200 for p in partes) and len(partes) > 1


@pytest.mark.asyncio
async def test_el_tts_se_puede_interrumpir():
    tts = MockTts()
    import asyncio

    tarea = asyncio.create_task(tts.speak("una respuesta larga " * 20))
    await asyncio.sleep(0.12)
    await tts.stop()
    assert await tarea is False


@pytest.mark.asyncio
async def test_el_tts_termina_si_no_se_interrumpe():
    assert await MockTts().speak("hola") is True
