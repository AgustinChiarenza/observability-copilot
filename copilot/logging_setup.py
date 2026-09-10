"""Logs en JSON cuando corre en un contenedor, legibles cuando corre en tu terminal.

El default es JSON porque el destino real de estos logs es el stack de logging
del cliente (Loki, Elastic, CloudWatch), y ahí una línea de texto libre es un
blob que nadie puede filtrar. `COPILOT_LOG_FORMAT=text` es para cuando lo estás
mirando vos.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fila = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            fila["exc"] = self.formatException(record.exc_info)
        return json.dumps(fila, ensure_ascii=False, default=str)


def setup(level: str | None = None) -> None:
    nivel = (level or os.getenv("COPILOT_LOG_LEVEL", "INFO")).upper()
    formato = os.getenv("COPILOT_LOG_FORMAT", "json").lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if formato == "json"
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s",
                               datefmt="%H:%M:%S"))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, nivel, logging.INFO))

    # httpx loguea cada request en INFO. Contra el TSDB del cliente son cientos
    # por minuto y tapan todo lo demás.
    logging.getLogger("httpx").setLevel(logging.WARNING)
