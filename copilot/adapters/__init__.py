"""Registro de adapters: el único lugar donde el core aprende nombres propios.

Un adapter se registra con un decorador y desde entonces existe para el YAML.
Agregar uno es escribir un archivo acá y no tocar nada más — ni el core, ni la
config, ni las rutas. Ese es el criterio con el que se mide si el diseño de
puertos está bien: si para sumar un backend hay que editar un `if` en otro lado,
está mal.

Los adapters se importan al final del módulo, no arriba: cada uno importa su
librería propia y así un `httpx` faltante rompe sólo su adapter, con un mensaje
que dice cuál, en vez de tumbar el registro entero.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

Kind = str  # "metrics" | "cost" | "model" | "notify"

_REGISTRY: dict[Kind, dict[str, Callable[..., Any]]] = {}

T = TypeVar("T")


class UnknownAdapter(KeyError):
    """El YAML pide un adapter que no existe. El mensaje lista los que sí."""


def register(kind: Kind, name: str) -> Callable[[T], T]:
    """Decorador de registro. `kind` es el puerto que implementa."""

    def _wrap(factory: T) -> T:
        bucket = _REGISTRY.setdefault(kind, {})
        if name in bucket:
            raise RuntimeError(f"adapter {kind}/{name} registrado dos veces")
        bucket[name] = factory  # type: ignore[assignment]
        return factory

    return _wrap


def available(kind: Kind) -> list[str]:
    return sorted(_REGISTRY.get(kind, {}))


def catalog() -> dict[Kind, list[str]]:
    """Todo lo instalado. Lo imprime `copilot adapters`."""
    return {k: sorted(v) for k, v in sorted(_REGISTRY.items())}


def build(
    kind: Kind, adapter: str, options: dict[str, Any] | None = None, **extra: Any,
) -> Any:
    """Instancia un adapter. Los errores de opciones los levanta él, no esto.

    El parámetro se llama `adapter` y no `name` a propósito: los adapters
    reciben un `name` propio —el alias de esa instancia en el YAML, que es lo
    que aparece en los logs y en el ruteo— y si este se llamara igual, pasarlo
    en `extra` chocaría con el posicional. Se descubrió así, con un
    `TypeError: got multiple values for argument 'name'` al armar el runtime.
    """
    bucket = _REGISTRY.get(kind, {})
    if adapter not in bucket:
        raise UnknownAdapter(
            f"No existe el adapter {kind}/'{adapter}'. Disponibles: "
            f"{', '.join(available(kind)) or '(ninguno)'}.")
    return bucket[adapter](**(options or {}), **extra)


def _load_builtins() -> None:
    """Importa los adapters que vienen en la caja, tolerando faltantes."""
    for mod in (
        "metrics_prometheus",
        "cost_promql",
        "model_openai",
        "notify_webhook",
        "notify_log",
    ):
        try:
            __import__(f"{__name__}.{mod}")
        except Exception as e:
            logger.warning("adapters: %s no se pudo cargar (%s: %s)", mod, type(e).__name__, e)


_load_builtins()
