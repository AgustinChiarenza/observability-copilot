"""Canal que escribe en el log. El default cuando no hay ninguno configurado.

Existe para que el sistema nunca se quede sin destino: sin un canal por defecto,
un error de tipeo en el YAML de notificaciones produce un producto que parece
sano, no falla, y no avisa nada. Que quede en el log convierte ese silencio en
algo que se puede encontrar con un grep.

También es el canal de los tests y del `--dry-run`.
"""
from __future__ import annotations

import logging
from typing import Any

from ..ports.notify import Delivery, Message, Severity
from . import register

logger = logging.getLogger("copilot.notify")

_NIVEL = {
    Severity.CRITICAL: logging.ERROR,
    Severity.WARNING: logging.WARNING,
    Severity.INFO: logging.INFO,
    Severity.RESOLVED: logging.INFO,
}


@register("notify", "log")
class LogNotifier:
    def __init__(self, *, max_chars: int = 4000, name: str = "log", **_ignored: Any):
        self.name = name
        self.max_chars = int(max_chars)
        #: Los tests leen de acá en vez de capturar logs.
        self.sent: list[Message] = []

    async def send(self, message: Message) -> Delivery:
        self.sent.append(message)
        logger.log(_NIVEL.get(message.severity, logging.INFO),
                   "[%s] %s — %s", message.severity, message.title,
                   message.body[: self.max_chars].replace("\n", " ")[:400])
        return Delivery(self.name, True, "logged")

    async def check(self) -> None:
        return
