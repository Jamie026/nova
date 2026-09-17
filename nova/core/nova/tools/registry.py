"""Registro de herramientas: la única puerta entre el modelo y la máquina.

El LLM nunca ejecuta nada. Propone {nombre, argumentos} y aquí se comprueba, en
este orden:
  1. la herramienta existe y está en la allowlist
  2. los argumentos validan contra su JSON Schema
  3. el nivel de riesgo permite ejecución automática o exige confirmación
  4. la ejecución respeta un timeout
  5. el resultado queda auditado

Cualquier fallo se convierte en una observación textual que vuelve al modelo, en
lugar de romper la sesión: el agente puede corregirse solo.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from jsonschema import Draft7Validator

from ..logging_setup import audit

log = logging.getLogger("nova.tools")


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(slots=True)
class ToolResult:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_observation(self) -> str:
        return self.summary if self.ok else f"ERROR: {self.summary}"


Handler = Callable[..., Awaitable[ToolResult]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    risk: Risk = Risk.LOW
    timeout_s: float = 45.0
    # Texto de confirmación: una plantilla con {argumentos} o una función, cuando
    # la frase depende de lo que se vaya a hacer («…y lo envío» sólo si send=true).
    confirm_template: str | Callable[[dict[str, Any]], str] = ""
    # Acciones que pregunta SIEMPRE, se configure lo que se configure: las que
    # salen de la máquina hacia otra persona (enviar un mensaje, un archivo).
    # Puede depender de los argumentos: pegar un archivo sin enviarlo no sale.
    always_confirm: bool | Callable[[dict[str, Any]], bool] = False

    def is_outward(self, arguments: dict[str, Any]) -> bool:
        if callable(self.always_confirm):
            try:
                return bool(self.always_confirm(arguments))
            except Exception:
                return True   # ante la duda, pregunta
        return bool(self.always_confirm)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def _with_defaults(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Los argumentos que el modelo omitió toman el default del esquema.

        Sin esto, una plantilla que menciona un campo opcional se rompería con
        KeyError justo cuando más importa: al pedir permiso para algo peligroso.
        """
        merged = {
            key: spec["default"]
            for key, spec in (self.parameters.get("properties") or {}).items()
            if isinstance(spec, dict) and "default" in spec
        }
        merged.update(arguments)
        return merged

    def confirmation_text(self, arguments: dict[str, Any]) -> str:
        filled = self._with_defaults(arguments)
        if callable(self.confirm_template):
            try:
                return self.confirm_template(filled)
            except Exception:  # una confirmación rota no debe saltarse la confirmación
                log.exception("plantilla de confirmación inválida en %s", self.name)
        elif self.confirm_template:
            try:
                return self.confirm_template.format(**filled)
            except (KeyError, IndexError):
                pass
        return f"¿Confirmas ejecutar «{self.name}»?"


class ToolValidationError(Exception):
    pass


class ToolRegistry:
    def __init__(self, allowlist: list[str] | None = None,
                 risk_policy: dict[str, str] | None = None,
                 auto_approve: list[str] | None = None):
        self._tools: dict[str, Tool] = {}
        self._allowlist = set(allowlist or [])
        self._policy = {"low": "auto", "medium": "confirm", "high": "confirm", **(risk_policy or {})}
        # Herramientas concretas que no preguntan, por nombre. Es más fino que
        # bajar toda la política de riesgo medio a "auto": el usuario puede
        # querer que Nova navegue por la pantalla sin pedir permiso y a la vez
        # que siga preguntando antes de cerrar una app con trabajo sin guardar.
        self._auto_approve = set(auto_approve or [])

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"herramienta duplicada: {tool.name}")
        Draft7Validator.check_schema(tool.parameters)
        self._tools[tool.name] = tool

    def register_all(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def enabled(self) -> list[Tool]:
        if not self._allowlist:
            return list(self._tools.values())
        return [t for t in self._tools.values() if t.name in self._allowlist]

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.enabled()]

    def needs_confirmation(self, tool: Tool, arguments: dict[str, Any] | None = None) -> bool:
        """Qué se pregunta y qué no.

        La regla original era «riesgo alto = pregunta siempre, sin excepción».
        En uso real eso significaba confirmar «2+2» en la calculadora, y el
        usuario pidió que no. Así que el alto se puede poner en "auto"... con
        una línea que la configuración no cruza: lo que sale hacia otra persona
        —un mensaje, un archivo enviado— pregunta siempre, porque no se deshace
        y porque el que lo recibe no es quien configuró Nova.
        """
        if tool.is_outward(arguments or {}):
            return True
        if tool.name in self._auto_approve and tool.risk is not Risk.HIGH:
            return False
        return self._policy.get(tool.risk.value, "confirm") != "auto"

    def validate(self, name: str, arguments: dict[str, Any]) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolValidationError(f"la herramienta «{name}» no existe")
        if self._allowlist and name not in self._allowlist:
            raise ToolValidationError(f"la herramienta «{name}» está deshabilitada en la configuración")
        errors = sorted(Draft7Validator(tool.parameters).iter_errors(arguments), key=lambda e: e.path)
        if errors:
            detail = "; ".join(f"{'.'.join(str(p) for p in e.path) or 'raíz'}: {e.message}" for e in errors[:3])
            raise ToolValidationError(f"argumentos inválidos para «{name}»: {detail}")
        return tool

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        started = time.perf_counter()
        try:
            tool = self.validate(name, arguments)
        except ToolValidationError as exc:
            audit(log, "tool_rejected", tool=name, reason=str(exc))
            return ToolResult(False, str(exc))

        audit(log, "tool_start", tool=name, args=arguments, risk=tool.risk.value)
        try:
            result = await asyncio.wait_for(tool.handler(**arguments), timeout=tool.timeout_s)
        except asyncio.TimeoutError:
            result = ToolResult(False, f"«{name}» superó el tiempo límite de {tool.timeout_s:.0f} s")
        except Exception as exc:
            log.exception("fallo ejecutando %s", name)
            result = ToolResult(False, f"{type(exc).__name__}: {exc}")
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        audit(log, "tool_end", tool=name, ok=result.ok, ms=elapsed_ms, summary=result.summary[:180])
        return result
