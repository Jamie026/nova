from .base import OsAdapter, WindowInfo, AppInfo  # noqa: F401


def build_os_adapter() -> OsAdapter:
    import sys

    if sys.platform == "win32":
        from .windows import WindowsAdapter

        return WindowsAdapter()
    from .unsupported import UnsupportedAdapter

    return UnsupportedAdapter()
