"""Herramientas de archivos.

El agente no navega el disco: pide nombres y recibe rutas. Todas las rutas que
entran por argumento pasan por `searcher.allows()` antes de tocarse, así que el
alcance efectivo es el de `files.search_roots` aunque el modelo se invente una
ruta absoluta.

Deliberadamente **no hay borrar, mover ni renombrar** en esta versión. Un agente
de voz que se equivoca al entender un nombre y borra el archivo equivocado es un
desastre sin deshacer; abrir el archivo equivocado no lo es. Cuando se añadan,
irán con riesgo alto y papelera de reciclaje, nunca borrado permanente.
"""
from __future__ import annotations

from pathlib import Path

from ..files.search import EXTENSIONS, FileSearcher, extensions_for
from ..os_adapter.base import OsAdapter
from .registry import Risk, Tool, ToolResult

KINDS = ["any", *sorted(EXTENSIONS.keys())]

# Tope de lectura: lo que cabe en el contexto sin arruinar la latencia.
MAX_READ_CHARS = 6000


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


# Windows no abre estos archivos: los ejecuta, y pueden venir de una descarga.
_RUNS_CODE = frozenset({
    ".appref-ms", ".bat", ".cmd", ".com", ".cpl", ".exe", ".hta", ".jar", ".js", ".jse",
    ".lnk", ".msc", ".msi", ".msp", ".pif", ".ps1", ".reg", ".scf", ".scr", ".url",
    ".vbe", ".vbs", ".wsf", ".wsh",
})


def _runs_code(path: str) -> bool:
    # Windows ignora los puntos y espacios finales: «setup.exe.» ejecuta setup.exe.
    return Path(str(path).rstrip(" .")).suffix.lower() in _RUNS_CODE


def _resolved(searcher, path: str) -> str:
    """La ruta tal cual, o resuelta dentro de las carpetas permitidas si es relativa."""
    resolve = getattr(searcher, "resolve", None)
    return str(resolve(path)) if resolve is not None else path


def build(os_adapter: OsAdapter, searcher: FileSearcher) -> list[Tool]:
    def _denied(path: str) -> ToolResult:
        return ToolResult(
            False,
            f"«{path}» está fuera de las carpetas que puedo tocar. "
            "Amplía files.search_roots en los ajustes si de verdad lo necesitas.",
        )

    async def find_files(name: str, kind: str = "any", folder: str = "") -> ToolResult:
        hits = searcher.search(name, kind=kind, folder=folder or None)
        if not hits:
            where = folder or "tus carpetas"
            return ToolResult(True, f"No encontré nada parecido a «{name}» en {where}",
                              {"results": []})
        best = hits[0]
        # Con la ruta completa: el modelo la usa tal cual en la siguiente
        # herramienta. Con sólo el nombre de la carpeta, inventaba una relativa.
        if len(hits) == 1:
            summary = f"Encontré «{best.name}»: {best.path}"
        else:
            others = "; ".join(h.path for h in hits[1:4])
            summary = (f"Encontré {len(hits)}. La mejor coincidencia: {best.path}. "
                       f"Otras: {others}")
        return ToolResult(True, summary, {"results": [h.as_dict() for h in hits]})

    async def list_folder(folder: str, kind: str = "any") -> ToolResult:
        path = Path(folder).expanduser()
        path = _resolved(searcher, path)
        if not searcher.allows(path):
            return _denied(folder)
        if not path.is_dir():
            return ToolResult(False, f"«{folder}» no es una carpeta")
        wanted = extensions_for(kind)
        entries = []
        for child in sorted(path.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            if child.is_file() and wanted is not None and child.suffix.lower() not in wanted:
                continue
            entries.append({"name": child.name, "path": str(child),
                            "is_dir": child.is_dir()})
            if len(entries) >= 60:
                break
        names = ", ".join(e["name"] for e in entries[:8]) or "nada"
        return ToolResult(True, f"{len(entries)} elementos en {path.name}: {names}",
                          {"entries": entries})

    async def user_folders() -> ToolResult:
        folders = os_adapter.user_folders()
        listing = ", ".join(f"{k}: {v}" for k, v in folders.items())
        return ToolResult(True, f"Carpetas del usuario — {listing}", {"folders": folders})

    async def open_file(path: str) -> ToolResult:
        path = _resolved(searcher, path)
        if not searcher.allows(path):
            return _denied(path)
        target = Path(path).expanduser()
        if not target.exists():
            return ToolResult(False, f"«{target.name}» ya no está en esa ruta")
        ok = os_adapter.open_path(str(target))
        return ToolResult(ok, f"Abrí {target.name}" if ok else f"No pude abrir {target.name}")

    async def reveal_file(path: str) -> ToolResult:
        path = _resolved(searcher, path)
        if not searcher.allows(path):
            return _denied(path)
        ok = os_adapter.reveal_path(str(Path(path).expanduser()))
        return ToolResult(ok, "Lo tienes seleccionado en el Explorador" if ok
                          else "No pude abrir el Explorador ahí")

    async def copy_file_to_clipboard(path: str, as_image: bool = False) -> ToolResult:
        path = _resolved(searcher, path)
        if not searcher.allows(path):
            return _denied(path)
        target = Path(path).expanduser()
        if not target.is_file():
            return ToolResult(False, f"«{path}» no es un archivo")
        if as_image:
            ok = os_adapter.copy_image_to_clipboard(str(target))
            mode = "como imagen"
        else:
            ok = os_adapter.copy_files_to_clipboard([str(target)])
            mode = "como archivo"
        return ToolResult(ok, f"Copié {target.name} al portapapeles {mode}" if ok
                          else f"No pude copiar {target.name}",
                          {"path": str(target), "as_image": as_image})

    async def read_text_file(path: str) -> ToolResult:
        path = _resolved(searcher, path)
        if not searcher.allows(path):
            return _denied(path)
        target = Path(path).expanduser()
        if not target.is_file():
            return ToolResult(False, f"«{path}» no es un archivo")
        try:
            text = target.read_text("utf-8", errors="replace")[:MAX_READ_CHARS]
        except OSError as exc:
            return ToolResult(False, f"No pude leer {target.name}: {exc}")
        stat = target.stat()
        return ToolResult(True, f"{target.name} ({_human_size(stat.st_size)}):\n{text}",
                          {"path": str(target), "text": text})

    kind_schema = {"type": "string", "enum": KINDS, "default": "any"}

    return [
        Tool(
            name="find_files",
            description=(
                "Busca archivos por nombre en las carpetas del usuario (Escritorio, Documentos, "
                "Descargas, Imágenes, Vídeos, Música). Devuelve rutas ordenadas por parecido. "
                "Úsala siempre antes de abrir, copiar o adjuntar un archivo: nunca inventes rutas."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nombre o parte del nombre, sin extensión"},
                    "kind": kind_schema,
                    "folder": {"type": "string", "description": "Carpeta concreta donde buscar (opcional)"},
                },
                "required": ["name"], "additionalProperties": False,
            },
            handler=find_files, risk=Risk.LOW, timeout_s=20,
        ),
        Tool(
            name="list_folder",
            description="Lista el contenido de una carpeta concreta del usuario.",
            parameters={
                "type": "object",
                "properties": {"folder": {"type": "string"}, "kind": kind_schema},
                "required": ["folder"], "additionalProperties": False,
            },
            handler=list_folder, risk=Risk.LOW, timeout_s=15,
        ),
        Tool(
            name="user_folders",
            description="Devuelve las rutas reales de Escritorio, Documentos, Descargas, Imágenes, Vídeos y Música.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=user_folders, risk=Risk.LOW, timeout_s=8,
        ),
        Tool(
            name="open_file",
            description="Abre un archivo o carpeta con la aplicación predeterminada del sistema.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False,
            },
            handler=open_file, risk=Risk.LOW, timeout_s=15,
            # Abrir no se pregunta, ejecutar sí: open_path es os.startfile.
            always_confirm=lambda args: _runs_code(args.get("path", "")),
            confirm_template="«{path}» es un programa y se ejecutaría. ¿Lo abro?",
        ),
        Tool(
            name="reveal_file",
            description="Abre el Explorador de Windows con el archivo seleccionado.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False,
            },
            handler=reveal_file, risk=Risk.LOW, timeout_s=15,
        ),
        Tool(
            name="copy_file_to_clipboard",
            description=(
                "Copia un archivo al portapapeles. Por defecto lo copia como archivo (igual que "
                "Ctrl+C en el Explorador), que es lo que hay que hacer para adjuntarlo en Discord, "
                "WhatsApp o un correo. Con as_image=true copia el contenido de la imagen como mapa "
                "de bits, para pegarlo dentro de Paint o Word."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "as_image": {"type": "boolean", "default": False},
                },
                "required": ["path"], "additionalProperties": False,
            },
            handler=copy_file_to_clipboard, risk=Risk.MEDIUM, timeout_s=20,
            confirm_template="¿Copio «{path}» al portapapeles?",
        ),
        Tool(
            name="read_text_file",
            description="Lee el contenido de un archivo de texto (txt, md, csv, código). No sirve para PDF ni Word.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False,
            },
            handler=read_text_file, risk=Risk.MEDIUM, timeout_s=15,
            confirm_template="¿Leo el contenido de «{path}»?",
        ),
    ]
