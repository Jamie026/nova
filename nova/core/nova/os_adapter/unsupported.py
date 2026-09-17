"""Adaptador vacío para plataformas aún no implementadas (macOS, Linux)."""
from __future__ import annotations

import sys

from .base import AppInfo, WindowInfo


class UnsupportedAdapter:
    name = sys.platform

    def _fail(self):
        raise NotImplementedError(f"Control del sistema no implementado para {sys.platform}")

    def list_installed_apps(self, refresh: bool = False) -> list[AppInfo]:
        return []

    def find_app(self, query: str) -> AppInfo | None:
        return None

    def open_app(self, app: AppInfo) -> bool:
        self._fail()

    def close_app(self, query: str) -> int:
        self._fail()

    def is_running(self, query: str) -> bool:
        return False

    def list_windows(self) -> list[WindowInfo]:
        return []

    def focus_window(self, handle: int) -> bool:
        self._fail()

    def set_window_state(self, handle: int, state: str) -> bool:
        self._fail()

    def minimize_all(self) -> bool:
        self._fail()

    def set_volume(self, percent: int) -> bool:
        self._fail()

    def media_key(self, action: str) -> bool:
        self._fail()

    def get_clipboard(self) -> str:
        return ""

    def set_clipboard(self, text: str) -> bool:
        self._fail()

    def window_rect(self, handle: int) -> tuple[int, int, int, int] | None:
        return None

    def user_folders(self) -> dict[str, str]:
        from pathlib import Path

        home = Path.home()
        return {
            key: str(home / name)
            for key, name in (("desktop", "Desktop"), ("documents", "Documents"),
                              ("downloads", "Downloads"), ("pictures", "Pictures"),
                              ("videos", "Videos"), ("music", "Music"))
            if (home / name).is_dir()
        }

    def open_path(self, path: str) -> bool:
        self._fail()

    def reveal_path(self, path: str) -> bool:
        self._fail()

    def copy_files_to_clipboard(self, paths: list[str]) -> bool:
        self._fail()

    def copy_image_to_clipboard(self, path: str) -> bool:
        self._fail()

    def type_text(self, text: str) -> bool:
        self._fail()

    def press_keys(self, combo: list[str]) -> bool:
        self._fail()

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> bool:
        self._fail()

    def screen_size(self) -> tuple[int, int]:
        return (0, 0)
