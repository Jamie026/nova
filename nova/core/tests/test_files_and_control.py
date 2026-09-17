"""Pruebas de archivos, alcance y control del escritorio.

Todo lo que se prueba aquí es determinista y sin Windows delante: la puntuación
de nombres, la puerta de alcance de rutas, la validación de combinaciones de
teclas y el texto de confirmación. Es a propósito — son las cuatro piezas donde
un fallo silencioso tiene consecuencias (abrir el archivo equivocado, salirse de
las carpetas permitidas, enviar una tecla arbitraria, o pedir permiso para algo
distinto de lo que se va a hacer).

La inyección de entrada real no se prueba aquí: eso necesita una máquina con
sesión gráfica y se verifica a mano con el guion de humo del README.
"""
from __future__ import annotations

import pytest

from nova.capture.screen import safe_filename
from nova.files.search import FileSearcher, extensions_for, is_within, score_name
from nova.tools.control_tools import validate_combo
from nova.tools.matching import score_window
from nova.tools.registry import Risk, Tool
from nova.os_adapter.base import WindowInfo


# --------------------------------------------------------------- puntuación
def test_nombre_exacto_gana_a_todo():
    assert score_name("Corin_Wickes.png", "corin wickes") > score_name("corin-2.png", "corin wickes")


def test_acentos_y_mayusculas_son_lo_mismo():
    assert score_name("Informe Anual.pdf", "informe anual") >= 0.98
    assert score_name("Sesión.md", "sesion") >= 0.98


def test_transcripcion_imperfecta_sigue_encontrando():
    """El STT devuelve lo que oye: «wikes» debe seguir apuntando a «Wickes»."""
    assert score_name("Corin_Wickes.webp", "corin wikes") >= 0.60


def test_nombre_que_no_tiene_nada_que_ver_puntua_bajo():
    assert score_name("presupuesto-2026.xlsx", "corin") < 0.60


def test_prefijo_gana_a_subcadena():
    assert score_name("corin-retrato.png", "corin") > score_name("retrato-corin.png", "corin")


# -------------------------------------------------------------- alcance
def test_ruta_dentro_de_la_raiz(tmp_path):
    root = tmp_path / "Documentos"
    (root / "sub").mkdir(parents=True)
    assert is_within(root / "sub" / "a.txt", [root])


def test_ruta_fuera_de_la_raiz_se_rechaza(tmp_path):
    root = tmp_path / "Documentos"
    root.mkdir()
    otra = tmp_path / "Windows"
    otra.mkdir()
    assert not is_within(otra / "system32.dll", [root])


def test_escalar_con_dos_puntos_no_se_escapa(tmp_path):
    """`Documentos/../Windows` resuelve fuera de la raíz y debe rechazarse."""
    root = tmp_path / "Documentos"
    root.mkdir()
    (tmp_path / "Windows").mkdir()
    assert not is_within(root / ".." / "Windows" / "x.dll", [root])


def test_el_buscador_solo_permite_lo_que_cuelga_de_sus_raices(tmp_path):
    root = tmp_path / "Imagenes"
    root.mkdir()
    (root / "foto.png").write_bytes(b"x")
    searcher = FileSearcher([str(root)])
    assert searcher.allows(root / "foto.png")
    assert not searcher.allows(tmp_path / "otra" / "foto.png")


# -------------------------------------------------------------- búsqueda
def test_busqueda_encuentra_y_filtra_por_tipo(tmp_path):
    root = tmp_path / "Imagenes"
    root.mkdir()
    (root / "Corin_Wickes.png").write_bytes(b"x")
    (root / "Corin_Wickes.txt").write_text("x")
    searcher = FileSearcher([str(root)])

    imagenes = searcher.search("corin wickes", kind="image")
    assert [h.name for h in imagenes] == ["Corin_Wickes.png"]

    todo = searcher.search("corin wickes")
    assert len(todo) == 2


def test_busqueda_no_sale_de_las_raices(tmp_path):
    root = tmp_path / "Imagenes"
    root.mkdir()
    fuera = tmp_path / "AppData"
    fuera.mkdir()
    (fuera / "secreto.png").write_bytes(b"x")
    assert FileSearcher([str(root)]).search("secreto") == []


def test_carpetas_de_ruido_no_se_recorren(tmp_path):
    root = tmp_path / "Proyectos"
    ruido = root / "node_modules" / "paquete"
    ruido.mkdir(parents=True)
    (ruido / "config.json").write_text("{}")
    assert FileSearcher([str(root)]).search("config") == []


def test_a_igualdad_de_parecido_gana_el_mas_reciente(tmp_path):
    import os
    import time

    root = tmp_path / "Descargas"
    (root / "sub").mkdir(parents=True)
    viejo = root / "recibo.pdf"
    nuevo = root / "sub" / "recibo.pdf"   # mismo nombre, otra carpeta
    viejo.write_bytes(b"x")
    nuevo.write_bytes(b"x")
    ayer = time.time() - 86400
    os.utime(viejo, (ayer, ayer))

    hits = FileSearcher([str(root)]).search("recibo")
    assert hits[0].path == str(nuevo)


def test_categoria_desconocida_falla_cerrada():
    """Una categoría que no existe no debe convertirse en «cualquier archivo»."""
    assert extensions_for("inventada") == set()
    assert extensions_for("any") is None
    assert extensions_for(None) is None
    assert ".png" in extensions_for("image")


# ------------------------------------------------------- teclas y ventanas
def test_combinacion_valida_se_normaliza():
    assert validate_combo(["Ctrl", " V "]) == ["ctrl", "v"]


@pytest.mark.parametrize("combo", [[], ["ctrl", "alt", "shift", "win", "a"], ["ctrl", "menu_contextual"]])
def test_combinaciones_invalidas_se_rechazan(combo):
    with pytest.raises(ValueError):
        validate_combo(combo)


def test_ventana_se_encuentra_por_proceso_aunque_el_titulo_sea_otro():
    discord = WindowInfo(handle=1, title="#general | Mi servidor - Discord", process="Discord.exe")
    chrome = WindowInfo(handle=2, title="Google Chrome", process="chrome.exe")
    assert score_window(discord, "discord") > score_window(chrome, "discord")
    assert score_window(discord, "discord") >= 0.95


# ------------------------------------------------------------ confirmación
def test_la_confirmacion_rellena_los_defaults_del_esquema():
    """Un campo opcional omitido no debe romper el texto del permiso."""
    tool = Tool(
        name="demo", description="", handler=None, risk=Risk.HIGH,
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "send": {"type": "boolean", "default": False}},
            "required": ["path"],
        },
        confirm_template="¿Pego {path} (enviar={send})?",
    )
    assert tool.confirmation_text({"path": "a.png"}) == "¿Pego a.png (enviar=False)?"


def test_la_confirmacion_puede_depender_de_los_argumentos():
    tool = Tool(
        name="demo", description="", handler=None, risk=Risk.HIGH,
        parameters={"type": "object", "properties": {"send": {"type": "boolean", "default": False}}},
        confirm_template=lambda args: "envío" if args.get("send") else "sin enviar",
    )
    assert tool.confirmation_text({}) == "sin enviar"
    assert tool.confirmation_text({"send": True}) == "envío"


def test_una_plantilla_rota_no_se_traga_la_confirmacion():
    """Si la plantilla falla, se pide permiso igual con el texto genérico."""
    tool = Tool(
        name="peligrosa", description="", handler=None, risk=Risk.HIGH,
        parameters={"type": "object", "properties": {}},
        confirm_template=lambda args: 1 / 0,
    )
    assert "peligrosa" in tool.confirmation_text({})


# --------------------------------------------------------------- capturas
def test_el_nombre_de_captura_es_seguro():
    name = safe_filename("Discord: canal #general/prueba")
    assert "/" not in name and ":" not in name and name.endswith(".png")


def test_nombre_de_captura_vacio_tiene_respaldo():
    assert safe_filename("").startswith("captura")


# ------------------------------------ regresiones reportadas en uso real
def test_la_palabra_activadora_sobrevive_a_que_whisper_la_parta():
    """Whisper transcribe «Nova» en español como «no va» muy a menudo.

    No se instancia WhisperKeywordSpotter porque su __init__ carga el modelo;
    se prueba su método de coincidencia, que es donde estaba el fallo.
    """
    from nova.audio.wakeword import WhisperKeywordSpotter

    spotter = WhisperKeywordSpotter.__new__(WhisperKeywordSpotter)
    spotter._threshold = 0.72
    spotter.set_keywords("nova")
    match = spotter._matches

    for heard in ["Nova", "nova.", "¿Nova?", "no va", "No va.", "Nova, abre spotify", "Noba"]:
        assert match(heard), f"no detectó «{heard}»"

    for heard in ["hola qué tal", "pon música", "Gracias por ver el video"]:
        assert not match(heard), f"falso positivo con «{heard}»"


def test_el_mensaje_de_asistente_se_sintetiza_si_el_proveedor_no_lo_da():
    """Sin esto el mensaje `tool` queda huérfano y el modelo repite la llamada."""
    from nova.agent.orchestrator import _assistant_message
    from nova.llm.base import LlmResponse, ToolCall

    response = LlmResponse(tool_calls=[ToolCall(id="c1", name="open_application",
                                                arguments={"application": "spotify"})])
    message = _assistant_message(response)
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["id"] == "c1"
    assert message["tool_calls"][0]["function"]["name"] == "open_application"


def test_el_historial_conserva_el_par_assistant_tool():
    from nova.agent.memory import ConversationMemory
    from nova.agent.orchestrator import _assistant_message
    from nova.llm.base import LlmResponse, ToolCall

    memory = ConversationMemory(10)
    memory.add_user("abre spotify")
    memory.add(_assistant_message(LlmResponse(
        tool_calls=[ToolCall(id="c1", name="open_application", arguments={"application": "spotify"})])))
    memory.add({"role": "tool", "tool_call_id": "c1", "name": "open_application", "content": "Abrí Spotify"})

    rendered = memory.messages()
    assert rendered[-1]["role"] == "tool", "el resultado de la herramienta se perdió"


def test_el_mock_corta_la_orden_en_la_conjuncion():
    from nova.llm.mock_llm import _first_target

    assert _first_target("navegador y busca vuelos a cusco") == "navegador"
    assert _first_target("spotify") == "spotify"
