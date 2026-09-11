"""Qué consultó el agente contra los datos del cliente, y cuándo.

Es la respuesta a la pregunta que hace cualquier revisión: "¿qué le pidió esto
exactamente a nuestro Prometheus?". Sin un registro, la respuesta honesta es
"no sé", y con eso no se aprueba una instalación en producción.

Se registran los argumentos completos —la query PromQL tal cual salió— pero
**no el resultado**: el resultado son los datos del cliente, y guardarlos acá
sería hacer una copia de sus métricas en un buffer que nadie pidió. Queda el
tamaño y si salió bien, que es lo que hace falta para auditar.

Anillo en memoria, y si la instalación configuró `storage.path`, cada entrada
se anexa también a un JSONL y las últimas se recargan al arrancar. Sin eso
queda en memoria, y `/v1/audit` lo dice (`durable: false`) para que nadie lo
confunda con un registro durable.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from ..store import Jsonl

MAX_ENTRIES = 1_000


@dataclass(frozen=True)
class Entry:
    ts: str
    actor: str
    tool: str
    args: dict[str, Any]
    ok: bool
    ms: int
    result_chars: int = 0
    error: str = ""
    run_id: str = ""
    labels: dict[str, str] = field(default_factory=dict)


_entries: deque[Entry] = deque(maxlen=MAX_ENTRIES)
_lock = Lock()
_store: Jsonl | None = None


def attach(store: Jsonl | None) -> int:
    """Engancha el archivo y recarga lo que había. Devuelve cuántas entradas
    recuperó. Con `None` vuelve a memoria sola."""
    global _store
    with _lock:
        _entries.clear()
        _store = store
        if store is None:
            return 0
        campos = {f for f in Entry.__dataclass_fields__}
        for fila in store.tail(MAX_ENTRIES):
            try:
                _entries.append(Entry(**{k: v for k, v in fila.items() if k in campos}))
            except TypeError:
                continue   # una línea de otra versión; se saltea, no se rompe
        return len(_entries)


def record(
    *,
    tool: str,
    args: dict[str, Any],
    ok: bool,
    ms: int,
    actor: str = "anonymous",
    result_chars: int = 0,
    error: str = "",
    run_id: str = "",
) -> Entry:
    e = Entry(
        ts=datetime.now(UTC).isoformat(),
        actor=actor, tool=tool, args=dict(args or {}), ok=ok, ms=ms,
        result_chars=result_chars, error=error[:300], run_id=run_id,
    )
    with _lock:
        _entries.append(e)
        store = _store
    if store is not None:
        store.append(asdict(e))
    return e


def timeline(limit: int = 100, *, tool: str = "", only_errors: bool = False) -> list[dict]:
    """Del más reciente al más viejo."""
    with _lock:
        filas = list(_entries)
    filas.reverse()
    if tool:
        filas = [e for e in filas if e.tool == tool]
    if only_errors:
        filas = [e for e in filas if not e.ok]
    return [asdict(e) for e in filas[:limit]]


def summary() -> dict[str, Any]:
    with _lock:
        filas = list(_entries)
    por_tool: dict[str, int] = {}
    for e in filas:
        por_tool[e.tool] = por_tool.get(e.tool, 0) + 1
    return {
        "entries": len(filas),
        "capacity": MAX_ENTRIES,
        "errors": sum(1 for e in filas if not e.ok),
        "by_tool": dict(sorted(por_tool.items(), key=lambda kv: -kv[1])),
        "oldest": filas[0].ts if filas else None,
        "newest": filas[-1].ts if filas else None,
        "durable": _store is not None,
    }


def reset() -> None:
    global _store
    with _lock:
        _entries.clear()
        _store = None
