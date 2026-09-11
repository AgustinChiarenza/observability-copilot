"""Persistencia mínima: archivos, no una base.

Un contenedor que se enchufa en el stack de otro no puede pedir un Postgres
para guardar tres cosas. Lo que hace falta durable es poco y tiene forma de
log: la auditoría (qué se consultó), las alertas que llegaron (qué sonó y qué
se dijo), y el estado del despachante (a quién se le avisó y cuántas veces
hoy). Un JSONL por cada log, un JSON para el estado, en un volumen.

Lo del despachante es lo que más importa persistir, aunque parezca lo menos:
sin eso, un reinicio a las 3 de la mañana olvida el tope diario y el dedup, y
lo primero que hace el proceso nuevo es volver a mandar todo lo que el viejo
ya había frenado. La persistencia acá no es comodidad, es la política que
sobrevive al reinicio.

Sin `storage.path` configurado todo queda en memoria, como hasta ahora, y los
endpoints lo dicen (`durable: false`).
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


class Jsonl:
    """Un log en disco: append de a una línea, lectura de las últimas N.

    El archivo crece sin límite y se rota por tamaño, no por fecha: cuando pasa
    `max_bytes` se renombra a `.1` y se empieza otro. Una sola generación
    atrás, a propósito — es un buffer de operación, no un archivo histórico
    que alguien vaya a auditar en tres años.
    """

    def __init__(self, path: Path, *, max_bytes: int = 20_000_000):
        self.path = path
        self._max = max_bytes
        self._lock = Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, obj: dict[str, Any]) -> None:
        linea = json.dumps(obj, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                if self.path.exists() and self.path.stat().st_size > self._max:
                    os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(linea)
            except OSError as e:
                # Un disco lleno no puede frenar una alerta ni un turno: se
                # loguea y se sigue en memoria.
                logger.warning("store: no se pudo escribir %s (%s)", self.path, e)

    def tail(self, n: int) -> list[dict[str, Any]]:
        """Las últimas `n` líneas, en orden. Lee entero el archivo actual: son
        megabytes como mucho, y se hace una vez, al arrancar."""
        salida: deque[dict[str, Any]] = deque(maxlen=n)
        for p in (self.path.with_suffix(self.path.suffix + ".1"), self.path):
            if not p.exists():
                continue
            try:
                with p.open(encoding="utf-8") as f:
                    for linea in f:
                        try:
                            salida.append(json.loads(linea))
                        except json.JSONDecodeError:
                            continue   # una línea cortada por un apagado a mitad
            except OSError as e:
                logger.warning("store: no se pudo leer %s (%s)", p, e)
        return list(salida)


class State:
    """Un JSON chico que se reescribe entero. Escritura atómica: se escribe al
    lado y se renombra, así un corte a mitad no deja medio archivo."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("store: %s ilegible, se arranca vacío (%s)", self.path, e)
            return {}

    def save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with self._lock:
            try:
                tmp.write_text(json.dumps(data, ensure_ascii=False, default=str),
                               encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as e:
                logger.warning("store: no se pudo guardar %s (%s)", self.path, e)


class Storage:
    """Los archivos de una instalación, o nada."""

    def __init__(self, root: str | Path | None):
        self.root = Path(root) if root else None

    @property
    def durable(self) -> bool:
        return self.root is not None

    def jsonl(self, name: str) -> Jsonl | None:
        return Jsonl(self.root / f"{name}.jsonl") if self.root else None

    def state(self, name: str) -> State | None:
        return State(self.root / f"{name}.json") if self.root else None
