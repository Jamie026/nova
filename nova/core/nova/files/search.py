"""Búsqueda de archivos por nombre en las carpetas del usuario.

Decisiones que importan:

* **Se recorre el disco, no se indexa.** Un índice propio se queda obsoleto,
  hay que mantenerlo y duplica lo que el sistema ya sabe. Un recorrido acotado
  (carpetas del usuario, profundidad limitada, tope de tiempo) resuelve
  «busca la imagen que se llama corin» en decenas de milisegundos en un SSD, que
  es todo lo que necesita una orden de voz.
* **El alcance es una lista blanca, no una lista negra.** Sólo se mira dentro de
  las carpetas declaradas en `files.search_roots` (Escritorio, Documentos,
  Descargas, Imágenes, Vídeos, Música y OneDrive por defecto). Nada fuera de ahí
  existe para el agente, aunque el modelo lo pida por su nombre completo: así un
  prompt malicioso no puede pasearse por `C:\\Windows` ni por AppData.
* **Nunca se devuelve contenido, sólo rutas.** Leer un archivo es otra
  herramienta, con su propio nivel de riesgo.

Todo lo de este módulo es determinista y sin dependencias del sistema
operativo, que es lo que permite probarlo sin Windows delante.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

# Categorías que el usuario nombra al hablar («una imagen», «un documento»).
EXTENSIONS: dict[str, set[str]] = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg", ".heic", ".avif"},
    "document": {".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt", ".pptx", ".ppt",
                 ".xlsx", ".xls", ".csv"},
    "video": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv"},
    "audio": {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"},
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".cs", ".go", ".rs",
             ".html", ".css", ".json", ".yml", ".yaml", ".sql", ".sh", ".ps1"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz"},
}

# Carpetas que nunca aportan nada a una búsqueda hablada y sí cuestan segundos.
SKIP_DIRS = {
    "appdata", "node_modules", ".git", ".venv", "venv", "__pycache__", "$recycle.bin",
    "system volume information", ".cache", "temp", "tmp", "dist", "build", ".next",
    ".gradle", ".idea", ".vscode", "site-packages", "windows", "program files",
    "program files (x86)", "programdata", "$windows.~ws",
}


def normalize(text: str) -> str:
    """Minúsculas sin acentos: «Corín» y «corin» deben ser la misma consulta."""
    table = str.maketrans("áéíóúüñ", "aeiouun")
    return text.lower().translate(table).strip()


def score_name(filename: str, query: str) -> float:
    """0..1 de cuánto se parece un nombre de archivo a lo que pidió el usuario.

    El escalón importa más que el valor exacto: coincidencia exacta del nombre
    sin extensión gana siempre, luego prefijo, luego subcadena y por último
    parecido difuso, que es lo que salva los errores de transcripción del STT
    («corin wikes» → «Corin_Wickes»).
    """
    stem = normalize(Path(filename).stem)
    target = normalize(query)
    if not target:
        return 0.0
    if stem == target:
        return 1.0
    # Los separadores de nombre de archivo son ruido al comparar con habla.
    flat_stem = stem.replace("_", " ").replace("-", " ").replace(".", " ")
    flat_target = target.replace("_", " ").replace("-", " ")
    if flat_stem == flat_target:
        return 0.98
    if flat_stem.startswith(flat_target):
        return 0.92
    if flat_target in flat_stem:
        return 0.86
    words = set(flat_stem.split())
    query_words = [w for w in flat_target.split() if w]
    if query_words and all(w in words for w in query_words):
        return 0.84
    return SequenceMatcher(None, flat_stem, flat_target).ratio() * 0.80


def is_within(path: Path, roots: list[Path]) -> bool:
    """True si `path` cuelga de alguna raíz permitida (tras resolver enlaces)."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            if resolved == root.resolve() or resolved.is_relative_to(root.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def extensions_for(kind: str | None) -> set[str] | None:
    """Extensiones de una categoría. None significa «cualquiera».

    Falla cerrada a propósito: una categoría desconocida devuelve el conjunto
    vacío (no encuentra nada) en lugar de None (encuentra todo). El esquema JSON
    ya limita el valor a la lista válida, pero si algún día se amplía por otro
    camino, el fallo seguro es no devolver archivos de más.
    """
    if kind is None or kind == "" or kind == "any":
        return None
    return EXTENSIONS.get(kind, set())


@dataclass(slots=True)
class FileHit:
    path: str
    name: str
    folder: str
    size_bytes: int
    modified: float
    score: float

    def as_dict(self) -> dict:
        return {
            "path": self.path, "name": self.name, "folder": self.folder,
            "size_bytes": self.size_bytes, "modified": self.modified,
            "score": round(self.score, 3),
        }


class FileSearcher:
    def __init__(self, roots: list[str], max_results: int = 12,
                 max_depth: int = 6, budget_s: float = 6.0):
        self.roots = [Path(r).expanduser() for r in roots if r]
        self.max_results = max_results
        self.max_depth = max_depth
        self.budget_s = budget_s

    def existing_roots(self) -> list[Path]:
        return [r for r in self.roots if r.is_dir()]

    def resolve(self, path: str | Path) -> Path:
        """Una ruta relativa («Prueba Nova\\clientes.xlsx») se busca dentro de las
        carpetas permitidas. Pasó: el modelo usó la carpeta que le dio la búsqueda
        sin la ruta completa y la herramienta no encontraba el archivo."""
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            return candidate
        for root in self.existing_roots():
            if (root / candidate).exists():
                return root / candidate
        for root in self.existing_roots():          # un nivel más abajo: Descargas\Prueba Nova\…
            found = next(root.glob(f"*/{candidate.as_posix()}"), None)
            if found is not None:
                return found
        return candidate

    def allows(self, path: str | Path) -> bool:
        """Puerta única de alcance: cualquier herramienta que toque una ruta pasa por aquí."""
        return is_within(Path(path).expanduser(), self.existing_roots())

    def search(self, query: str, kind: str | None = None,
               folder: str | None = None, min_score: float = 0.60) -> list[FileHit]:
        """Devuelve los mejores parecidos ordenados por puntuación y recencia."""
        wanted = extensions_for(kind)
        roots = self.existing_roots()
        if folder:
            candidate = Path(folder).expanduser()
            if not is_within(candidate, roots) or not candidate.is_dir():
                return []
            roots = [candidate]

        deadline = time.monotonic() + self.budget_s
        hits: list[FileHit] = []

        for root in roots:
            base_depth = len(root.parts)
            for current, dirnames, filenames in os.walk(root, topdown=True):
                if time.monotonic() > deadline:
                    break
                here = Path(current)
                depth = len(here.parts) - base_depth
                if depth >= self.max_depth:
                    dirnames[:] = []
                else:
                    dirnames[:] = [
                        d for d in dirnames
                        if d.lower() not in SKIP_DIRS and not d.startswith(".")
                    ]
                for filename in filenames:
                    if wanted is not None and Path(filename).suffix.lower() not in wanted:
                        continue
                    score = score_name(filename, query)
                    if score < min_score:
                        continue
                    full = here / filename
                    try:
                        stat = full.stat()
                    except OSError:
                        continue
                    hits.append(FileHit(
                        path=str(full), name=filename, folder=str(here),
                        size_bytes=stat.st_size, modified=stat.st_mtime, score=score,
                    ))

        # A igualdad de parecido gana el más reciente: si tienes tres versiones
        # de la misma imagen, la que acabas de guardar es casi siempre la buena.
        hits.sort(key=lambda h: (round(h.score, 2), h.modified), reverse=True)
        return hits[:self.max_results]
