from nova.agent.memory import ActionLog, ActionRecord, ConversationMemory


def test_el_historial_conserva_el_orden():
    memory = ConversationMemory(max_turns=4)
    memory.add_user("abre spotify")
    memory.add_assistant("Listo.")
    roles = [m["role"] for m in memory.messages()]
    assert roles == ["user", "assistant"]


def test_descarta_mensajes_tool_huerfanos():
    memory = ConversationMemory(max_turns=4)
    memory.add({"role": "tool", "tool_call_id": "perdido", "content": "resultado"})
    memory.add_user("hola")
    assert [m["role"] for m in memory.messages()] == ["user"]


def test_conserva_tool_con_su_assistant():
    memory = ConversationMemory(max_turns=4)
    memory.add({"role": "assistant", "content": "",
                "tool_calls": [{"id": "abc", "type": "function",
                                "function": {"name": "open_application", "arguments": "{}"}}]})
    memory.add({"role": "tool", "tool_call_id": "abc", "content": "Abrí Spotify"})
    assert [m["role"] for m in memory.messages()] == ["assistant", "tool"]


def test_el_registro_de_acciones_se_limita():
    log = ActionLog(max_actions=2)
    for i in range(5):
        log.add(ActionRecord("open_application", {"application": f"app{i}"}, True, "ok"))
    assert len(log.recent(10)) == 2
    assert "app4" in log.render()


def test_render_vacio_es_legible():
    assert "ninguna" in ActionLog().render()
