"""Punto de entrada del núcleo.

    python -m nova.main              # normal
    python -m nova.main --mock       # sin APIs ni modelos pesados
    python -m nova.main --text "abre spotify"   # un turno y salir
"""
from __future__ import annotations

import argparse
import os

# Las barras de progreso de tqdm (Chatterbox, huggingface) salen por stderr y
# la interfaz las muestra como líneas de log. Se apagan antes de importar nada.
os.environ.setdefault("TQDM_DISABLE", "1")
import asyncio
import contextlib
import logging
import signal
import sys

from dotenv import load_dotenv

from .app import NovaApp
from .config import Config
from .logging_setup import setup_logging


async def _serve(app: NovaApp) -> None:
    stop = asyncio.Event()

    def _request_stop(*_args) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, _request_stop)
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, _request_stop)

    await app.start()
    try:
        await asyncio.wait({asyncio.create_task(stop.wait()), asyncio.create_task(app.stop_requested.wait())},
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        await app.shutdown()


async def _one_shot(app: NovaApp, text: str) -> None:
    await app.ipc.start()
    turn = await app.orchestrator.run(text)
    print(f"\nNova: {turn.reply}  [{turn.steps} paso(s)]\n")
    await app.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(prog="nova", description="Núcleo de Nova")
    parser.add_argument("--mock", action="store_true", help="usa proveedores simulados")
    parser.add_argument("--text", help="ejecuta una sola instrucción escrita y sale")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    load_dotenv()
    config = Config.load()
    if args.mock:
        config.set("dev.mock", True)
    if args.log_level:
        config.set("dev.log_level", args.log_level)
    setup_logging(config.get("dev.log_level", "INFO"))

    log = logging.getLogger("nova")
    log.info("Nova %s — modo %s", __import__("nova").__version__, "simulado" if config.mock else "real")

    app = NovaApp(config)
    runner = _one_shot(app, args.text) if args.text else _serve(app)
    try:
        asyncio.run(runner)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
