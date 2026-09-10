"""NotifyPort: sacar el mensaje por el canal que el cliente ya usa.

Un canal es tonto a propósito: recibe un `Message` y lo entrega. **No decide si
había que mandarlo.** Esa decisión —dedup, histéresis, horario de silencio, tope
diario— vive una capa más arriba y es la misma para todos los canales, porque el
error que te desinstala no es "el adapter de Slack tenía un bug": es mandar 200
SMS una madrugada. Si cada canal decidiera por su cuenta, ese límite habría que
implementarlo (y romperlo) cinco veces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class Severity(StrEnum):
    """Las mismas cuatro que usa Alertmanager, para no traducir en la frontera."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class Message:
    """Lo que se entrega.

    `title` y `body` van separados porque los canales los tratan distinto: un
    SMS es sólo el título más una línea, Slack quiere las dos partes y un mail
    quiere asunto y cuerpo. Que el canal recorte, no el que redacta.
    """

    title: str
    body: str
    severity: Severity = Severity.INFO
    #: Estable entre disparos de la misma alerta. Es la llave del dedup.
    fingerprint: str = ""
    url: str = ""
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Delivery:
    channel: str
    ok: bool
    detail: str = ""


@runtime_checkable
class NotifyPort(Protocol):
    name: str

    #: Un SMS no puede con 3.000 caracteres. El canal declara su techo y el
    #: despachante recorta antes de entregar, en vez de que cada adapter
    #: invente su propio recorte.
    max_chars: int

    async def send(self, message: Message) -> Delivery:
        ...

    async def check(self) -> None:
        """Valida configuración y alcance del canal, sin mandar nada."""
        ...
