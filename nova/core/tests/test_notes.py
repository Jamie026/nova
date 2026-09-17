"""La memoria persistente: lo que se guarda sobrevive al reinicio y no se duplica."""
from nova.agent.notes import UserNotes


def test_sobrevive_a_un_reinicio(tmp_path):
    ruta = tmp_path / "memory.json"
    UserNotes(ruta).add("Usa Opera GX como navegador")
    otra_sesion = UserNotes(ruta)
    assert otra_sesion.all() == ["Usa Opera GX como navegador"]


def test_no_duplica_ni_con_mayusculas_ni_con_tildes(tmp_path):
    notas = UserNotes(tmp_path / "m.json")
    assert notas.add("Llámame Jamie")
    assert not notas.add("llamame jamie")
    assert len(notas.all()) == 1


def test_olvidar_por_fragmento(tmp_path):
    notas = UserNotes(tmp_path / "m.json")
    notas.add("Usa Opera GX como navegador")
    notas.add("Prefiere Spotify para música")
    assert notas.remove("opera") == 1
    assert notas.all() == ["Prefiere Spotify para música"]
    assert notas.remove("nada de esto") == 0


def test_tope_de_notas_descarta_la_menos_usada(tmp_path):
    from nova.agent import notes as mod
    notas = UserNotes(tmp_path / "m.json")
    for i in range(mod.MAX_NOTES):
        notas.add(f"nota {i}")
    notas.add("nota 0")            # la refresca: no debe caerse
    notas.add("nota nueva")
    assert len(notas.all()) == mod.MAX_NOTES
    assert "nota 0" in notas.all() and "nota 1" not in notas.all()


def test_archivo_corrupto_no_impide_arrancar(tmp_path):
    ruta = tmp_path / "m.json"
    ruta.write_text("{esto no es json", encoding="utf-8")
    assert UserNotes(ruta).all() == []


def test_se_ve_en_el_prompt(tmp_path):
    from nova.agent.prompts import build_system_prompt
    notas = UserNotes(tmp_path / "m.json"); notas.add("Usa Opera GX")
    prompt = build_system_prompt("es", "hoy", "(n)", "(n)", notes=notas.render())
    assert "Lo que sé del usuario:\n- Usa Opera GX" in prompt
