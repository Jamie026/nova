"""Separar lo que Nova dice de lo que muestra.

Lo que más importa aquí no es el caso bonito, es lo que pasa cuando el modelo no
obedece el formato: Nova tiene que seguir hablando, aunque sea en un solo idioma.
"""
import pytest

from nova.agent.bilingual import split_bilingual


def test_separa_el_formato_pedido():
    hablado, mostrado = split_bilingual(
        "EN: Done, I opened Spotify.\nES: Listo, abrí Spotify.")
    assert hablado == "Done, I opened Spotify."
    assert mostrado == "Listo, abrí Spotify."


def test_aguanta_el_orden_cambiado():
    hablado, mostrado = split_bilingual(
        "ES: Listo, abrí Spotify.\nEN: Done, I opened Spotify.")
    assert hablado == "Done, I opened Spotify."
    assert mostrado == "Listo, abrí Spotify."


@pytest.mark.parametrize("texto", [
    "**EN:** Hi there\n**ES:** Hola",
    "EN - Hi there\nES - Hola",
    "en: Hi there\nes: Hola",
    "EN:  Hi there  \n\nES:  Hola  ",
])
def test_aguanta_las_variantes_del_modelo(texto):
    # Negritas, guiones, minúsculas y líneas en blanco: un modelo obedece el
    # formato casi siempre, y "casi" no basta cuando el fallo deja a Nova muda.
    assert split_bilingual(texto) == ("Hi there", "Hola")


def test_junta_las_lineas_de_una_misma_etiqueta():
    hablado, mostrado = split_bilingual(
        "EN: I found three files.\nThe newest one is open.\nES: Encontré tres archivos.")
    assert hablado == "I found three files. The newest one is open."
    assert mostrado == "Encontré tres archivos."


def test_sin_etiquetas_dice_y_muestra_lo_mismo():
    # El caso importante: el modelo ignoró el formato. Mejor hablar en un solo
    # idioma que quedarse callada.
    assert split_bilingual("Listo, abrí Spotify.") == ("Listo, abrí Spotify.",) * 2


def test_con_una_sola_etiqueta_usa_esa_para_todo():
    assert split_bilingual("EN: Done.") == ("Done.", "Done.")
    assert split_bilingual("ES: Listo.") == ("Listo.", "Listo.")


def test_no_confunde_un_texto_que_empieza_con_esas_letras():
    entrada = "Enciende la luz del escritorio"
    assert split_bilingual(entrada) == (entrada, entrada)


def test_el_orquestador_no_parte_nada_si_hay_un_solo_idioma():
    from nova.agent.orchestrator import Orchestrator

    orq = Orchestrator.__new__(Orchestrator)
    orq._language, orq._speak_language = "es", ""
    # No parte nada; sólo une las líneas, como se dice en voz alta.
    assert orq._split("EN: Done.\nES: Listo.") == ("EN: Done. ES: Listo.",) * 2
    orq._speak_language = "en"
    assert orq._split("EN: Done.\nES: Listo.") == ("Done.", "Listo.")


@pytest.mark.asyncio
async def test_la_traduccion_de_la_confirmacion_no_toca_el_mensaje_literal():
    from nova.agent.orchestrator import Orchestrator
    from nova.llm.base import LlmResponse

    class LlmTraductor:
        async def chat(self, messages, tools=None):
            # Traduce el resto pero deja las marcas: es lo que se le pide.
            texto = messages[-1]["content"]
            return LlmResponse(text=texto.replace("¿Envío", "Do I send").replace(" a ", " to ").replace(" en ", " on "))

    orq = Orchestrator.__new__(Orchestrator)
    orq._language, orq._speak_language, orq._llm = "es", "en", LlmTraductor()
    hablado = await orq.translate_for_speech("¿Envío «nos vemos mañana» a «Alain Ryan» en Discord?")
    assert "«nos vemos mañana»" in hablado
    assert "«Alain Ryan»" in hablado
    assert hablado.startswith("Do I send")


@pytest.mark.asyncio
async def test_si_la_traduccion_pierde_el_mensaje_se_usa_la_original():
    from nova.agent.orchestrator import Orchestrator
    from nova.llm.base import LlmResponse

    class LlmQueSeComeLasMarcas:
        async def chat(self, messages, tools=None):
            return LlmResponse(text="Do I send it to him on Discord?")

    orq = Orchestrator.__new__(Orchestrator)
    orq._language, orq._speak_language, orq._llm = "es", "en", LlmQueSeComeLasMarcas()
    original = "¿Envío «hola» a «Alain Ryan» en Discord?"
    assert await orq.translate_for_speech(original) == original
