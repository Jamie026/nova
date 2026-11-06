"""Servidor WebSocket local: el núcleo publica eventos, la UI envía comandos.

WebSocket y no stdin/stdout porque el flujo es bidireccional, con eventos que
llegan sin petición previa (parciales de transcripción, cambios de estado) y
porque permite correr el núcleo headless y conectarse desde un script de prueba.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets
from websockets.server import WebSocketServerProtocol

log = logging.getLogger("nova.ipc")

CommandHandler = Callable[[dict[str, Any]], Awaitable[None]]


class IpcServer:
    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self._clients: set[WebSocketServerProtocol] = set()
        self._handlers: dict[str, CommandHandler] = {}
        self._server: Any = None

    def on(self, command: str, handler: CommandHandler) -> None:
        self._handlers[command] = handler

    async def start(self) -> None:
        # origins=[None]: sólo conexiones SIN cabecera Origin, que es como se conecta
        # la app (el cliente `ws` de Node). Un navegador siempre la envía, así que
        # una página web abierta en Opera no puede conectarse a 127.0.0.1:8765 y
        # mandarle órdenes a Nova («envía un WhatsApp a…»): se la rechaza con 403.
        self._server = await websockets.serve(
            self._handle_client, self._host, self._port, ping_interval=20, max_size=2 ** 20,
            origins=[None],
        )
        log.info("IPC escuchando en ws://%s:%s", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(self, ws: WebSocketServerProtocol) -> None:
        self._clients.add(ws)
        log.debug("UI conectada (%d activas)", len(self._clients))
        try:
            await ws.send(json.dumps({"type": "ready"}))
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                handler = self._handlers.get(message.get("type", ""))
                if handler is None:
                    log.debug("comando sin handler: %s", message.get("type"))
                    continue
                try:
                    await handler(message)
                except Exception:
                    log.exception("fallo procesando comando %s", message.get("type"))
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)

    async def broadcast(self, event: dict[str, Any]) -> None:
        if not self._clients:
            return
        payload = json.dumps(event, ensure_ascii=False)
        dead: list[WebSocketServerProtocol] = []
        for client in self._clients:
            try:
                await client.send(payload)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.discard(client)

    def broadcast_soon(self, event: dict[str, Any]) -> None:
        """Para llamar desde callbacks de audio sin await."""
        try:
            asyncio.get_running_loop().create_task(self.broadcast(event))
        except RuntimeError:
            pass
