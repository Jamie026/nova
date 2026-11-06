"""Abrir apps, archivos y carpetas no pregunta; ejecutar un programa sí."""
import pytest

from nova.config import DEFAULTS
from nova.tools import file_tools
from nova.tools.applications import build as build_apps
from nova.tools.registry import ToolRegistry


def _registry():
    registry = ToolRegistry(risk_policy=DEFAULTS["security"])
    registry.register_all(file_tools.build(None, None))
    registry.register_all(build_apps(None, []))
    return registry


@pytest.mark.parametrize("path", [
    r"C:\Users\ana\Documents\informe.pdf",
    r"C:\Users\ana\Downloads\foto.PNG",
    r"C:\Users\ana\Documents\notas.txt",
    r"C:\Users\ana\Documents\Proyectos",
])
def test_archivos_y_carpetas_se_abren_sin_preguntar(path):
    registry = _registry()
    assert not registry.needs_confirmation(registry.get("open_file"), {"path": path})


@pytest.mark.parametrize("path", [
    r"C:\Users\ana\Downloads\setup.exe",
    r"C:\Users\ana\Downloads\SETUP.EXE",
    r"C:\Users\ana\Downloads\setup.exe.",
    r"C:\Users\ana\Downloads\factura.pdf.exe",
    r"C:\Users\ana\Downloads\script.bat",
    r"C:\Users\ana\Downloads\macro.vbs",
    r"C:\Users\ana\Desktop\acceso.lnk",
])
def test_lo_que_se_ejecuta_al_abrirlo_sigue_preguntando(path):
    registry = _registry()
    tool = registry.get("open_file")
    assert registry.needs_confirmation(tool, {"path": path})
    assert "se ejecutaría" in tool.confirmation_text({"path": path})


def test_las_apps_y_el_explorador_no_preguntan():
    registry = _registry()
    assert not registry.needs_confirmation(registry.get("open_application"), {"application": "spotify"})
    assert not registry.needs_confirmation(registry.get("reveal_file"), {"path": r"C:\a\b.txt"})
