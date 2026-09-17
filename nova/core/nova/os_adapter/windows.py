"""Implementación para Windows 11.

Decisiones que importan:
  * El inventario de apps sale de `Get-StartApps`, que devuelve tanto Win32 como
    UWP con su AppID. Lanzar con `explorer.exe shell:AppsFolder\\<AppID>` funciona
    para ambos tipos sin necesidad de conocer la ruta del ejecutable.
  * Cerrar una app manda WM_CLOSE a sus ventanas primero (da oportunidad de
    guardar) y sólo mata el proceso si sigue vivo tras el timeout.
  * El volumen usa pycaw (API oficial de audio) en lugar de simular pulsaciones.
  * El teclado y el ratón se simulan con **SendInput** por ctypes, no con
    pyautogui. Son ~120 líneas, evitan arrastrar Pillow y pyscreeze, y sobre todo
    permiten escribir con KEYEVENTF_UNICODE: así se teclean tildes, eñes y emojis
    en cualquier aplicación sin depender de la distribución de teclado activa,
    que es justo donde fallan las alternativas.
  * Copiar un *archivo* al portapapeles (no su contenido) se hace con
    `Set-Clipboard -Path`, que deja un CF_HDROP igual que copiar desde el
    Explorador. Es lo que hace que pegar en Discord adjunte el archivo con su
    nombre original en lugar de incrustar un mapa de bits sin nombre.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import time
from ctypes import wintypes
from difflib import SequenceMatcher
from pathlib import Path

import psutil

from .base import AppInfo, WindowInfo

log = logging.getLogger("nova.os.windows")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE, SW_SHOW = 6, 3, 9, 5
WM_CLOSE = 0x0010

VK_MEDIA = {
    "play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
    "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
}

# ------------------------------------------------------------------ SendInput
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
MOUSEEVENTF = {
    "left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040),
}

ULONG_PTR = wintypes.WPARAM


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("value", _InputUnion)]


VK_NAMES: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "supr": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "capslock": 0x14,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
    **{chr(c): c for c in range(0x30, 0x3A)},          # 0-9
    **{chr(c + 32): c for c in range(0x41, 0x5B)},     # a-z -> VK de la letra
}

# Teclas que necesitan la marca de extendida para que las lea cualquier app.
EXTENDED_KEYS = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0x2C}


def _utf16_units(char: str) -> list[int]:
    """Unidades de código UTF-16 de un carácter (dos si está fuera del BMP)."""
    raw = char.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]

_NOISE = {"app", "aplicación", "programa", "el", "la", "los", "las", "de"}


def _normalize(text: str) -> str:
    table = str.maketrans("áéíóúüñ", "aeiouun")
    words = text.lower().translate(table).split()
    return " ".join(w for w in words if w not in _NOISE)


def _powershell(command: str, timeout: int = 25, sta: bool = False) -> str:
    # -STA hace falta para todo lo que toque el portapapeles vía System.Windows.Forms:
    # esas APIs son COM de apartamento único y fallan en silencio en MTA.
    args = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    if sta:
        args.append("-STA")
    args += ["-Command", command]
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        log.debug("powershell rc=%s: %s", result.returncode, result.stderr[:200])
    return result.stdout


def _ps_quote(value: str) -> str:
    """Comilla simple de PowerShell: duplicar la comilla es todo el escape que existe."""
    return "'" + str(value).replace("'", "''") + "'"


class WindowsAdapter:
    name = "windows"

    def __init__(self) -> None:
        self._apps: list[AppInfo] = []
        self._apps_loaded_at = 0.0

    # ---------------------------------------------------------------- apps
    def list_installed_apps(self, refresh: bool = False) -> list[AppInfo]:
        if self._apps and not refresh and time.time() - self._apps_loaded_at < 900:
            return self._apps
        raw = _powershell("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
        try:
            data = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            data = []
        if isinstance(data, dict):
            data = [data]
        self._apps = [
            AppInfo(name=item["Name"], app_id=item["AppID"],
                    kind="uwp" if "!" in item.get("AppID", "") else "win32")
            for item in data if item.get("Name") and item.get("AppID")
        ]
        self._apps_loaded_at = time.time()
        log.info("%d aplicaciones indexadas", len(self._apps))
        return self._apps

    def find_app(self, query: str) -> AppInfo | None:
        target = _normalize(query)
        if not target:
            return None
        best, best_score = None, 0.0
        for app in self.list_installed_apps():
            name = _normalize(app.name)
            if name == target:
                return app
            score = SequenceMatcher(None, name, target).ratio()
            if target in name.split() or name.startswith(target):
                score = max(score, 0.9)
            elif target in name:
                score = max(score, 0.8)
            if score > best_score:
                best, best_score = app, score
        return best if best_score >= 0.62 else None

    def open_app(self, app: AppInfo) -> bool:
        try:
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{app.app_id}"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True
        except Exception as exc:
            log.error("no se pudo abrir %s: %s", app.name, exc)
            return False

    def _matching_processes(self, query: str) -> list[psutil.Process]:
        target = _normalize(query)
        found: list[psutil.Process] = []
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = _normalize((proc.info["name"] or "").removesuffix(".exe"))
                if not name:
                    continue
                if name == target or target in name or SequenceMatcher(None, name, target).ratio() > 0.85:
                    found.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return found

    def close_app(self, query: str) -> int:
        procs = self._matching_processes(query)
        if not procs:
            return 0
        pids = {p.pid for p in procs}
        for window in self.list_windows():
            try:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(window.handle, ctypes.byref(pid))
                if pid.value in pids:
                    user32.PostMessageW(window.handle, WM_CLOSE, 0, 0)
            except Exception:
                continue
        gone, alive = psutil.wait_procs(procs, timeout=4)
        for proc in alive:
            try:
                proc.terminate()
            except psutil.Error:
                continue
        psutil.wait_procs(alive, timeout=3)
        return len(gone) + len(alive)

    def is_running(self, query: str) -> bool:
        return bool(self._matching_processes(query))

    # ------------------------------------------------------------- ventanas
    def list_windows(self) -> list[WindowInfo]:
        windows: list[WindowInfo] = []
        foreground = user32.GetForegroundWindow()

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title or title in ("Program Manager", "Windows Input Experience"):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                process = psutil.Process(pid.value).name()
            except psutil.Error:
                process = "?"
            windows.append(WindowInfo(
                handle=int(hwnd), title=title, process=process,
                minimized=bool(user32.IsIconic(hwnd)), foreground=(hwnd == foreground),
            ))
            return True

        user32.EnumWindows(callback, 0)
        return windows

    def focus_window(self, handle: int) -> bool:
        try:
            if user32.IsIconic(handle):
                user32.ShowWindow(handle, SW_RESTORE)
            # Windows bloquea SetForegroundWindow desde procesos en segundo plano;
            # enganchar el input del hilo en foreground es la vía soportada.
            current = kernel32.GetCurrentThreadId()
            target = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            user32.AttachThreadInput(target, current, True)
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
            user32.AttachThreadInput(target, current, False)
            return True
        except Exception as exc:
            log.error("no se pudo enfocar la ventana %s: %s", handle, exc)
            return False

    def set_window_state(self, handle: int, state: str) -> bool:
        mapping = {"minimize": SW_MINIMIZE, "maximize": SW_MAXIMIZE, "restore": SW_RESTORE, "show": SW_SHOW}
        if state not in mapping:
            return False
        user32.ShowWindow(handle, mapping[state])
        return True

    def minimize_all(self) -> bool:
        _powershell("(New-Object -ComObject Shell.Application).MinimizeAll()")
        return True

    # -------------------------------------------------------------- sistema
    def set_volume(self, percent: int) -> bool:
        try:
            from comtypes import CLSCTX_ALL
            from ctypes import POINTER, cast
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            volume.SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)
            return True
        except Exception as exc:
            log.error("no se pudo ajustar el volumen: %s", exc)
            return False

    def media_key(self, action: str) -> bool:
        code = VK_MEDIA.get(action)
        if code is None:
            return False
        user32.keybd_event(code, 0, 0, 0)
        user32.keybd_event(code, 0, 2, 0)
        return True

    def get_clipboard(self) -> str:
        try:
            import pyperclip

            return pyperclip.paste()
        except Exception:
            return ""

    def set_clipboard(self, text: str) -> bool:
        try:
            import pyperclip

            pyperclip.copy(text)
            return True
        except Exception:
            return False

    def window_rect(self, handle: int) -> tuple[int, int, int, int] | None:
        """Rectángulo real en pantalla, sin el margen invisible de las sombras DWM."""
        rect = wintypes.RECT()
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        try:
            hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(handle), ctypes.wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
                ctypes.byref(rect), ctypes.sizeof(rect),
            )
            if hr != 0:
                raise OSError(hr)
        except Exception:
            if not user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect)):
                return None
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    # ------------------------------------------------------------- archivos
    def user_folders(self) -> dict[str, str]:
        """Carpetas del usuario, incluidas las redirigidas a OneDrive."""
        home = Path.home()
        folders: dict[str, str] = {}
        candidates = {
            "desktop": ["Desktop", "Escritorio"],
            "documents": ["Documents", "Documentos"],
            "downloads": ["Downloads", "Descargas"],
            "pictures": ["Pictures", "Imágenes", "Imagenes"],
            "videos": ["Videos", "Vídeos"],
            "music": ["Music", "Música", "Musica"],
        }
        roots = [home]
        onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
        if onedrive and Path(onedrive).is_dir():
            roots.append(Path(onedrive))
        for key, names in candidates.items():
            for root in roots:
                for name in names:
                    path = root / name
                    if path.is_dir():
                        folders.setdefault(key, str(path))
                        break
                if key in folders:
                    break
        return folders

    def open_path(self, path: str) -> bool:
        try:
            os.startfile(path)  # noqa: S606 — asociación de archivo del propio Windows
            return True
        except Exception as exc:
            log.error("no se pudo abrir %s: %s", path, exc)
            return False

    def reveal_path(self, path: str) -> bool:
        try:
            subprocess.Popen(["explorer.exe", "/select,", str(Path(path))],
                             creationflags=subprocess.CREATE_NO_WINDOW)
            return True
        except Exception as exc:
            log.error("no se pudo mostrar %s en el Explorador: %s", path, exc)
            return False

    def copy_files_to_clipboard(self, paths: list[str]) -> bool:
        """Deja los archivos en el portapapeles como si los hubieras copiado del Explorador."""
        existing = [str(Path(p)) for p in paths if Path(p).exists()]
        if not existing:
            return False
        quoted = ",".join(_ps_quote(p) for p in existing)
        _powershell(f"Set-Clipboard -Path @({quoted})", timeout=15, sta=True)
        return True

    def copy_image_to_clipboard(self, path: str) -> bool:
        """Copia el contenido de la imagen como mapa de bits (para Paint, Word, etc.)."""
        if not Path(path).exists():
            return False
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            f"$img = [System.Drawing.Image]::FromFile({_ps_quote(str(Path(path)))}); "
            "[System.Windows.Forms.Clipboard]::SetImage($img); $img.Dispose()"
        )
        _powershell(script, timeout=20, sta=True)
        return True

    # ------------------------------------------------- entrada (teclado/ratón)
    @staticmethod
    def _send(inputs: list[_Input]) -> bool:
        if not inputs:
            return False
        array = (_Input * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), array, ctypes.sizeof(_Input))
        if sent != len(inputs):
            log.error("SendInput sólo entregó %d de %d eventos (error %s)",
                      sent, len(inputs), kernel32.GetLastError())
            return False
        return True

    @staticmethod
    def _key_event(vk: int, up: bool) -> _Input:
        flags = KEYEVENTF_KEYUP if up else 0
        if vk in EXTENDED_KEYS:
            flags |= KEYEVENTF_EXTENDEDKEY
        return _Input(type=INPUT_KEYBOARD,
                      value=_InputUnion(ki=_KeyboardInput(wVk=vk, wScan=0, dwFlags=flags,
                                                          time=0, dwExtraInfo=0)))

    @staticmethod
    def _unicode_event(code_unit: int, up: bool) -> _Input:
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
        return _Input(type=INPUT_KEYBOARD,
                      value=_InputUnion(ki=_KeyboardInput(wVk=0, wScan=code_unit, dwFlags=flags,
                                                          time=0, dwExtraInfo=0)))

    def type_text(self, text: str) -> bool:
        """Escribe texto carácter a carácter en la ventana que tenga el foco.

        Se envía en lotes para no saturar la cola de entrada de aplicaciones
        Electron (Discord, Slack, VS Code), que pierden caracteres si les llegan
        miles de eventos de golpe.
        """
        ok = True
        batch: list[_Input] = []
        for char in text:
            if char in ("\n", "\r"):
                batch += [self._key_event(0x0D, False), self._key_event(0x0D, True)]
            elif char == "\t":
                batch += [self._key_event(0x09, False), self._key_event(0x09, True)]
            else:
                # utf-16-le para que los caracteres fuera del BMP (emojis) viajen
                # como su par de sustitución, que es lo que espera KEYEVENTF_UNICODE.
                for code_unit in _utf16_units(char):
                    batch += [self._unicode_event(code_unit, False),
                              self._unicode_event(code_unit, True)]
            if len(batch) >= 80:
                ok = self._send(batch) and ok
                batch = []
                time.sleep(0.004)
        if batch:
            ok = self._send(batch) and ok
        return ok

    def press_keys(self, combo: list[str]) -> bool:
        """Pulsa una combinación (['ctrl','v']) respetando el orden de modificadores."""
        codes: list[int] = []
        for name in combo:
            vk = VK_NAMES.get(str(name).strip().lower())
            if vk is None:
                log.error("tecla desconocida: %s", name)
                return False
            codes.append(vk)
        events = [self._key_event(vk, False) for vk in codes]
        events += [self._key_event(vk, True) for vk in reversed(codes)]
        return self._send(events)

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> bool:
        down, up = MOUSEEVENTF.get(button, MOUSEEVENTF["left"])
        if not user32.SetCursorPos(int(x), int(y)):
            return False
        time.sleep(0.02)
        events: list[_Input] = []
        for _ in range(2 if double else 1):
            events += [
                _Input(type=INPUT_MOUSE, value=_InputUnion(mi=_MouseInput(0, 0, 0, down, 0, 0))),
                _Input(type=INPUT_MOUSE, value=_InputUnion(mi=_MouseInput(0, 0, 0, up, 0, 0))),
            ]
        return self._send(events)

    def screen_size(self) -> tuple[int, int]:
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        return (user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
