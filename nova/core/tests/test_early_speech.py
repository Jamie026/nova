"""La voz arranca en cuanto la parte hablada está completa, sin esperar el subtítulo."""
import pytest

from nova.agent.bilingual import spoken_so_far
from nova.agent.memory import ActionLog, ConversationMemory, Preferences
from nova.agent.orchestrator import Orchestrator
from nova.llm.base import LlmResponse
from nova.llm.deepseek import _response
from nova.tools.registry import ToolRegistry


@pytest.mark.parametrize("parcial, esperado", [
    ("EN: Hi there", None),                       # el subtítulo aún no empezó
    ("EN: Hi there\nES", None),                   # etiqueta a medio llegar
    ("EN: Hi there\nES:", "Hi there"),
    ("**EN:** Sure, opening it.\n**ES:** Cla", "Sure, opening it."),
    ("EN: One.\nMore.\nES: Uno.", "One. More."),
    ("ES: hola\nEN: hi", None),                   # orden invertido: se espera al final
    ("EN:\nES: hola", None),                      # sin texto hablado no hay nada que decir
    ("Sin etiquetas", None),
])
def test_parte_hablada_segun_llega(parcial, esperado):
    assert spoken_so_far(parcial) == esperado


class StreamingLlm:
    """Entrega la respuesta en trozos, como la API real."""

    def __init__(self, texto, trozo=7):
        self._texto = texto
        self._trozo = trozo

    async def chat(self, messages, tools=None, on_text=None):
        if on_text is not None:
            for fin in range(self._trozo, len(self._texto) + self._trozo, self._trozo):
                on_text(self._texto[:fin])
        return LlmResponse(text=self._texto)

    async def close(self):
        return None


class ClassicLlm:
    async def chat(self, messages, tools=None):
        return LlmResponse(text="EN: Hi.\nES: Hola.")

    async def close(self):
        return None


def build(llm):
    async def emit(event):
        return None

    async def confirm(request_id, text, risk):
        return True

    return Orchestrator(llm=llm, registry=ToolRegistry(), conversation=ConversationMemory(),
                        actions=ActionLog(), preferences=Preferences(), emit=emit,
                        confirm=confirm, language="es", speak_language="en")


async def test_adelanta_la_voz_y_el_resultado_coincide():
    agent = build(StreamingLlm("EN: Sure, what do you need?\nES: Claro, ¿qué necesitas?"))
    adelantado = []
    turno = await agent.run("hola", early_speak=adelantado.append)
    assert adelantado == ["Sure, what do you need?"]
    assert turno.spoken == "Sure, what do you need?"
    assert turno.reply == "Claro, ¿qué necesitas?"


async def test_no_adelanta_una_accion_que_no_hizo():
    # «Ya lo abrí» sin herramienta recibe el aviso del sistema y el modelo
    # rectifica: lo adelantado habría sido una mentira dicha en voz alta.
    class Rectifica(StreamingLlm):
        def __init__(self):
            super().__init__("EN: Done, I opened Spotify for you.\nES: Listo, abrí Spotify.")
            self.llamadas = 0

        async def chat(self, messages, tools=None, on_text=None):
            self.llamadas += 1
            if self.llamadas == 2:
                return LlmResponse(text="EN: I can't open apps from here.\nES: No puedo abrir apps desde aquí.")
            return await super().chat(messages, tools, on_text)

    agent = build(Rectifica())
    adelantado = []
    turno = await agent.run("abre spotify", early_speak=adelantado.append)
    assert adelantado == []
    assert turno.spoken == "I can't open apps from here."


async def test_un_proveedor_sin_streaming_sigue_funcionando():
    agent = build(ClassicLlm())
    adelantado = []
    turno = await agent.run("hola", early_speak=adelantado.append)
    assert adelantado == []
    assert turno.spoken == "Hi."


def test_las_llamadas_troceadas_se_reconstruyen():
    respuesta = _response("", [("call_1", "open_application", '{"application": "Spotify"}'),
                               ("call_2", "remember", "{no es json")])
    assert respuesta.wants_tools
    assert respuesta.tool_calls[0].arguments == {"application": "Spotify"}
    assert "__parse_error__" in respuesta.tool_calls[1].arguments
    assert respuesta.raw_message["tool_calls"][0]["function"]["name"] == "open_application"
