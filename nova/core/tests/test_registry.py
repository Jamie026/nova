import asyncio

import pytest

from nova.tools.registry import Risk, Tool, ToolRegistry, ToolResult, ToolValidationError

SCHEMA = {
    "type": "object",
    "properties": {"application": {"type": "string"}},
    "required": ["application"],
    "additionalProperties": False,
}


def make_registry(**kwargs) -> tuple[ToolRegistry, list]:
    llamadas = []

    async def handler(application: str) -> ToolResult:
        llamadas.append(application)
        return ToolResult(True, f"Abrí {application}")

    registry = ToolRegistry(**kwargs)
    registry.register(Tool("open_application", "abre una app", SCHEMA, handler, Risk.LOW))
    return registry, llamadas


@pytest.mark.asyncio
async def test_ejecuta_con_argumentos_validos():
    registry, llamadas = make_registry()
    result = await registry.execute("open_application", {"application": "spotify"})
    assert result.ok and llamadas == ["spotify"]


@pytest.mark.asyncio
async def test_rechaza_herramienta_inexistente():
    registry, _ = make_registry()
    result = await registry.execute("format_disk", {})
    assert not result.ok and "no existe" in result.summary


@pytest.mark.asyncio
async def test_rechaza_argumentos_invalidos_sin_ejecutar():
    registry, llamadas = make_registry()
    result = await registry.execute("open_application", {"app": "spotify"})
    assert not result.ok and llamadas == []


@pytest.mark.asyncio
async def test_allowlist_bloquea_herramientas_no_listadas():
    registry, _ = make_registry(allowlist=["browser_open"])
    with pytest.raises(ToolValidationError):
        registry.validate("open_application", {"application": "spotify"})
    assert registry.schemas() == []


@pytest.mark.asyncio
async def test_error_del_handler_se_convierte_en_observacion():
    async def boom() -> ToolResult:
        raise RuntimeError("el proceso murió")

    registry = ToolRegistry()
    registry.register(Tool("boom", "falla", {"type": "object", "properties": {}}, boom))
    result = await registry.execute("boom", {})
    assert not result.ok and "el proceso murió" in result.summary


@pytest.mark.asyncio
async def test_timeout_no_cuelga_la_sesion():
    async def lenta() -> ToolResult:
        await asyncio.sleep(5)
        return ToolResult(True, "nunca")

    registry = ToolRegistry()
    registry.register(Tool("lenta", "tarda", {"type": "object", "properties": {}}, lenta, timeout_s=0.1))
    result = await registry.execute("lenta", {})
    assert not result.ok and "tiempo límite" in result.summary


def test_lo_que_sale_hacia_otra_persona_confirma_aunque_la_politica_diga_auto():
    # La regla cambió a petición del usuario: el riesgo alto YA se puede poner en
    # "auto" (confirmar «2+2» en la calculadora era absurdo). Lo que no cambia es
    # que lo marcado como saliente —mensajes, archivos enviados— pregunta siempre.
    registry = ToolRegistry(risk_policy={"low": "auto", "medium": "auto", "high": "auto"})
    local = Tool("type_text", "escribe", {"type": "object", "properties": {}},
                 lambda: None, Risk.HIGH)
    saliente = Tool("send_message", "envía", {"type": "object", "properties": {}},
                    lambda: None, Risk.HIGH, always_confirm=True)
    registry.register_all([local, saliente])
    assert not registry.needs_confirmation(local)
    assert registry.needs_confirmation(saliente)
    # Y con la política por defecto, el alto sigue preguntando.
    assert ToolRegistry().needs_confirmation(local)


def test_riesgo_medio_respeta_la_politica():
    tool = Tool("close_application", "cierra", SCHEMA, lambda application: None, Risk.MEDIUM)
    auto = ToolRegistry(risk_policy={"medium": "auto"})
    auto.register(tool)
    assert not auto.needs_confirmation(tool)


def test_texto_de_confirmacion_usa_los_argumentos():
    tool = Tool("close_application", "cierra", SCHEMA, lambda application: None, Risk.MEDIUM,
                confirm_template="¿Cierro {application}?")
    assert tool.confirmation_text({"application": "Word"}) == "¿Cierro Word?"


def test_auto_approve_quita_la_pregunta_solo_a_las_herramientas_nombradas():
    from nova.tools.registry import Risk, Tool, ToolRegistry, ToolResult

    async def nada(**_):
        return ToolResult(True, "ok")

    def tool(name, risk):
        return Tool(name=name, description="x", parameters={"type": "object"}, handler=nada, risk=risk)

    reg = ToolRegistry(auto_approve=["read_screen", "send_message"])
    reg.register_all([tool("read_screen", Risk.MEDIUM), tool("close_app", Risk.MEDIUM),
                      tool("send_message", Risk.HIGH)])
    assert not reg.needs_confirmation(reg.get("read_screen"))    # nombrada: no pregunta
    assert reg.needs_confirmation(reg.get("close_app"))          # medio no nombrado: pregunta
    # Alto nombrado: pregunta igual. La lista no puede saltarse el riesgo alto.
    assert reg.needs_confirmation(reg.get("send_message"))


def test_con_todo_en_auto_lo_que_sale_hacia_otra_persona_sigue_preguntando():
    from nova.tools.registry import Risk, Tool, ToolRegistry, ToolResult

    async def nada(**_):
        return ToolResult(True, "ok")

    reg = ToolRegistry(risk_policy={"low": "auto", "medium": "auto", "high": "auto"})
    reg.register_all([
        Tool(name="type_text", description="x", parameters={"type": "object"}, handler=nada, risk=Risk.HIGH),
        Tool(name="send_message", description="x", parameters={"type": "object"}, handler=nada,
             risk=Risk.HIGH, always_confirm=lambda a: bool(a.get("send", True))),
        Tool(name="close_app", description="x", parameters={"type": "object"}, handler=nada, risk=Risk.MEDIUM),
    ])
    assert not reg.needs_confirmation(reg.get("type_text"), {})          # alto en auto: no pregunta
    assert not reg.needs_confirmation(reg.get("close_app"), {})
    assert reg.needs_confirmation(reg.get("send_message"), {"send": True})       # sale: pregunta
    assert reg.needs_confirmation(reg.get("send_message"), {})                   # send por defecto = true
    assert not reg.needs_confirmation(reg.get("send_message"), {"send": False})  # sólo escribe: no sale
