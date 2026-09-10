"""Qué puede hacer el agente. Hoy: leer, y nada más.

En el proyecto anterior este módulo tenía tres efectos (lectura / plan / apply)
porque mutaba infraestructura con Terraform. Acá hay uno solo, y esa reducción
es el producto: lo que se le vende a un cliente es "esto no puede tocar nada
tuyo", y esa frase tiene que ser verificable en un archivo de 60 líneas, no
inferible leyendo el repo entero.

Lo que se conserva del original es lo único que importaba: **default deny**. Una
tool que no está clasificada no se ejecuta. Agregar una tool nueva sin declarar
su efecto la deja bloqueada — que es el lado correcto para equivocarse, porque
el error se ve en el primer test y no en la cuenta del cliente.

Cuando llegue la remediación automática (no en v1, a propósito: el read-only
*es* la historia de confianza), el efecto `APPLY` vuelve acá con su gate de
aprobación, y este comentario es el lugar donde mirar por qué.
"""
from __future__ import annotations

from enum import StrEnum


class Effect(StrEnum):
    READ = "read"
    """Consulta y no cambia nada. El único efecto que existe hoy."""


class Denied(Exception):
    """La operación no está permitida. El mensaje dice qué hacer al respecto."""

    def __init__(self, reason: str, *, what: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.what = what


#: Efecto declarado de cada tool. La llave es el nombre exacto que ve el modelo.
_EFFECTS: dict[str, Effect] = {}


def declare(name: str, effect: Effect = Effect.READ) -> None:
    """Clasifica una tool. Lo llama el registro al dar de alta cada una."""
    _EFFECTS[name] = effect


def effect_of(name: str) -> Effect | None:
    """Efecto de una tool, o None si nadie la clasificó."""
    return _EFFECTS.get(name)


def classified() -> dict[str, str]:
    """Lo declarado, para el endpoint de estado y la auditoría."""
    return {k: str(v) for k, v in sorted(_EFFECTS.items())}


def verify(name: str) -> None:
    """Levanta `Denied` si la tool no puede ejecutarse.

    El nombre lo eligió el modelo y puede estar inventado —eso pasa seguido con
    modelos chicos—, así que este chequeo corre siempre antes de ejecutar, no
    sólo al armar el catálogo. Que el modelo no vea una tool no alcanza: puede
    pedirla igual.
    """
    efecto = _EFFECTS.get(name)
    if efecto is None:
        raise Denied(
            f"'{name}' no está clasificada, así que no se ejecuta. Si es una tool "
            f"nueva, registrala con `@tool(...)`; si el modelo la inventó, este "
            f"mensaje ya es la respuesta correcta.",
            what=name,
        )
    if efecto is not Effect.READ:
        raise Denied(
            f"'{name}' tiene efecto '{efecto}' y esta versión sólo permite lectura.",
            what=name,
        )
