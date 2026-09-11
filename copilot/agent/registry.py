"""Catálogo de tools: lo que el modelo puede pedir, y cómo se ejecuta.

Una tool se declara con un decorador y con eso queda hecho todo: aparece en el
catálogo que ve el modelo, queda clasificada en `permissions` y se puede
ejecutar. No hay una segunda lista que mantener en sincronía — esas listas
paralelas siempre se desincronizan, y el modo en que fallan es el peor posible:
una tool que el modelo ve pero no puede llamar, o peor, una que puede llamar
pero nadie revisó.

El `parameters` es JSON Schema tal cual, porque es lo que espera el campo
homónimo del function calling. No hay traducción de tipos en el medio.
"""
from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..ports.cost import CostPort
from ..ports.metrics import MetricsPort
from . import permissions

logger = logging.getLogger(__name__)


@dataclass
class Context:
    """Lo que una tool tiene a mano. Los puertos, nada más.

    Un puerto puede venir en None: un cliente puede no tener costos
    configurados, y eso no es un error de arranque. Las tools que lo necesitan
    lo dicen con un mensaje que el modelo puede transmitir, en vez de explotar.
    """

    metrics: MetricsPort | None = None
    cost: CostPort | None = None
    extras: dict[str, Any] = field(default_factory=dict)


ToolFn = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn

    def definition(self) -> dict[str, Any]:
        """Formato function calling de OpenAI."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip()[:1024],
                "parameters": self.parameters,
            },
        }


_TOOLS: dict[str, ToolSpec] = {}


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    *,
    effect: permissions.Effect = permissions.Effect.READ,
) -> Callable[[ToolFn], ToolFn]:
    """Da de alta una tool y la clasifica en un solo paso."""

    def _wrap(fn: ToolFn) -> ToolFn:
        if name in _TOOLS:
            raise RuntimeError(f"tool '{name}' registrada dos veces")
        if not inspect.iscoroutinefunction(fn):
            raise RuntimeError(f"tool '{name}' tiene que ser async")
        _TOOLS[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            fn=fn,
        )
        permissions.declare(name, effect)
        return fn

    return _wrap


def definitions(ctx: Context | None = None) -> list[dict[str, Any]]:
    """Catálogo para el modelo.

    Si viene el contexto, se ocultan las tools cuyo puerto no está configurado:
    ofrecerle al modelo una tool de costos en una instalación sin costos es
    garantizar que la pida, falle, y gaste un paso del presupuesto en
    descubrirlo.
    """
    salida = []
    for spec in _TOOLS.values():
        if ctx is not None and _needs_cost(spec) and ctx.cost is None:
            continue
        if ctx is not None and spec.name == "targets_health" and not getattr(
                ctx.metrics, "supports_targets", True):
            continue
        salida.append(spec.definition())
    return salida


def _needs_cost(spec: ToolSpec) -> bool:
    return spec.name.startswith("cost_")


def known(name: str) -> bool:
    return name in _TOOLS


def spec(name: str) -> ToolSpec | None:
    return _TOOLS.get(name)


async def execute(name: str, args: dict[str, Any], ctx: Context) -> dict[str, Any]:
    """Ejecuta una tool con el nombre y los argumentos que eligió el modelo.

    Devuelve siempre `{"status": ..., "result": ...}` y nunca levanta: un fallo
    de tool es información para el modelo —puede corregir la query y volver a
    intentar— y no motivo para cortar el turno del usuario.
    """
    try:
        permissions.verify(name)
    except permissions.Denied as e:
        logger.warning("agent: tool denegada '%s': %s", name, e.reason)
        return {"status": "error", "result": e.reason}

    spec_ = _TOOLS.get(name)
    if spec_ is None:  # pragma: no cover — permissions.verify ya lo cubre
        return {"status": "error", "result": f"tool desconocida: {name}"}

    try:
        salida = await spec_.fn(ctx, **(args or {}))
    except TypeError as e:
        # Argumentos que no encajan: casi siempre el modelo inventó un campo.
        # Devolverle el error de firma le alcanza para corregirse solo.
        return {"status": "error", "result": f"argumentos inválidos para {name}: {e}"}
    except Exception as e:
        logger.info("agent: tool '%s' falló: %s: %s", name, type(e).__name__, e)
        return {"status": "error", "result": f"{type(e).__name__}: {e}"}

    return {"status": "ok", "result": salida}


def reset_for_tests() -> None:
    _TOOLS.clear()
