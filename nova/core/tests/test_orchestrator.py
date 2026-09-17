import pytest

from nova.agent.memory import ActionLog, ConversationMemory, Preferences
from nova.agent.orchestrator import Orchestrator
from nova.llm.base import LlmResponse, ToolCall
from nova.tools.registry import Risk, Tool, ToolRegistry, ToolResult


class ScriptedLlm:
    def __init__(self, respuestas):
        self._respuestas = list(respuestas)
        self.recibidos = []

    async def chat(self, messages, tools=None):
        self.recibidos.append(messages)
        return self._respuestas.pop(0) if self._respuestas else LlmResponse(text="Listo.")

    async def close(self):
        return None


def build(registry, respuestas, aprobar=True):
    eventos = []

    async def emit(event):
        eventos.append(event)

    async def confirm(request_id, text, risk):
        eventos.append({"type": "confirm_request", "text": text})
        return aprobar

    agent = Orchestrator(
        llm=ScriptedLlm(respuestas), registry=registry,
        conversation=ConversationMemory(), actions=ActionLog(),
        preferences=Preferences(), emit=emit, confirm=confirm, max_steps=4,
    )
    return agent, eventos


def registry_con(nombre="open_application", risk=Risk.LOW):
    ejecutadas = []

    async def handler(application: str) -> ToolResult:
        ejecutadas.append(application)
        return ToolResult(True, f"Abrí {application}")

    registry = ToolRegistry(risk_policy={"medium": "confirm"})
    registry.register(Tool(nombre, "abre", {
        "type": "object", "properties": {"application": {"type": "string"}},
        "required": ["application"], "additionalProperties": False,
    }, handler, risk))
    return registry, ejecutadas


@pytest.mark.asyncio
async def test_usa_la_herramienta_y_despues_responde():
    registry, ejecutadas = registry_con()
    agent, _ = build(registry, [
        LlmResponse(tool_calls=[ToolCall("1", "open_application", {"application": "spotify"})],
                    raw_message={"role": "assistant", "content": "",
                                 "tool_calls": [{"id": "1", "type": "function",
                                                 "function": {"name": "open_application",
                                                              "arguments": '{"application":"spotify"}'}}]}),
        LlmResponse(text="Listo, abrí Spotify."),
    ])
    turn = await agent.run("abre spotify")
    assert ejecutadas == ["spotify"]
    assert turn.reply == "Listo, abrí Spotify."
    assert turn.steps == 2


@pytest.mark.asyncio
async def test_riesgo_medio_denegado_no_ejecuta():
    registry, ejecutadas = registry_con(risk=Risk.MEDIUM)
    agent, eventos = build(registry, [
        LlmResponse(tool_calls=[ToolCall("1", "open_application", {"application": "word"})],
                    raw_message={"role": "assistant", "content": "",
                                 "tool_calls": [{"id": "1", "type": "function",
                                                 "function": {"name": "open_application",
                                                              "arguments": '{"application":"word"}'}}]}),
        LlmResponse(text="De acuerdo, no lo hago."),
    ], aprobar=False)
    await agent.run("abre word")
    assert ejecutadas == []
    assert any(e["type"] == "confirm_request" for e in eventos)


@pytest.mark.asyncio
async def test_una_llamada_repetida_no_se_ejecuta_dos_veces():
    """Regresión: un modelo atascado repetía la misma acción hasta agotar los pasos.

    Con efectos secundarios reales (abrir una app ocho veces, enviar un mensaje
    ocho veces) eso no es sólo lento: es daño. La segunda llamada idéntica se
    responde con la observación anterior y el turno se cierra.
    """
    registry, ejecutadas = registry_con()
    bucle = LlmResponse(
        tool_calls=[ToolCall("1", "open_application", {"application": "spotify"})],
        raw_message={"role": "assistant", "content": "",
                     "tool_calls": [{"id": "1", "type": "function",
                                     "function": {"name": "open_application",
                                                  "arguments": '{"application":"spotify"}'}}]})
    agent, _ = build(registry, [bucle] * 10)
    turn = await agent.run("haz algo en bucle")

    assert ejecutadas == ["spotify"], "la herramienta se ejecutó más de una vez"
    assert turn.steps == 2, "no cortó en cuanto detectó la repetición"
    assert "pasos" not in turn.reply


@pytest.mark.asyncio
async def test_tope_de_pasos_corta_el_bucle():
    """Si cada paso hace algo distinto, el guardia no aplica y manda el tope."""
    registry, ejecutadas = registry_con()

    def paso(n: int) -> LlmResponse:
        return LlmResponse(
            tool_calls=[ToolCall(str(n), "open_application", {"application": f"app{n}"})],
            raw_message={"role": "assistant", "content": "",
                         "tool_calls": [{"id": str(n), "type": "function",
                                         "function": {"name": "open_application",
                                                      "arguments": '{"application":"app%d"}' % n}}]})

    agent, _ = build(registry, [paso(n) for n in range(10)])
    turn = await agent.run("haz muchas cosas distintas")
    assert turn.steps == 4
    assert "pasos" in turn.reply
    assert len(ejecutadas) == 4


@pytest.mark.asyncio
async def test_responde_sin_herramientas_cuando_no_hacen_falta():
    registry, ejecutadas = registry_con()
    agent, _ = build(registry, [LlmResponse(text="Son las tres.")])
    turn = await agent.run("qué hora es")
    assert turn.reply == "Son las tres." and ejecutadas == []
