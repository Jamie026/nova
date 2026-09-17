"""Rutas de datos del usuario. Centralizadas para no esparcir os.path por el código."""
from __future__ import annotations

import os
from pathlib import Path


def app_data_dir() -> Path:
    base = os.environ.get("NOVA_DATA_DIR")
    if base:
        root = Path(base)
    elif os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home())) / "Nova"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nova"
    root.mkdir(parents=True, exist_ok=True)
    return root


def settings_file() -> Path:
    return app_data_dir() / "settings.json"


def preferences_file() -> Path:
    return app_data_dir() / "preferences.json"


def memory_file() -> Path:
    """Lo que Nova sabe del usuario y conserva entre sesiones."""
    return app_data_dir() / "memory.json"


def logs_dir() -> Path:
    d = app_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def screenshots_dir() -> Path:
    """Las capturas van a Imágenes/Nova y no a AppData: son archivos del usuario,
    tiene que poder encontrarlas sin saber dónde guarda sus cosas la aplicación."""
    pictures = Path.home() / "Pictures"
    if not pictures.is_dir():
        pictures = Path.home() / "Imágenes"
    base = pictures if pictures.is_dir() else app_data_dir()
    d = base / "Nova"
    d.mkdir(parents=True, exist_ok=True)
    return d


def chrome_profile_dir() -> Path:
    d = app_data_dir() / "chrome-profile"
    d.mkdir(parents=True, exist_ok=True)
    return d
