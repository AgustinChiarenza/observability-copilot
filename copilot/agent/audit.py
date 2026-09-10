"""Qué consultó el agente contra los datos del cliente, y cuándo.

Es la respuesta a la pregunta que hace cualquier revisión: "¿qué le pidió esto
exactamente a nuestro Prometheus?". Sin un registro, la respuesta honesta es
"no sé", y con eso no se aprueba una instalación en producción.

Se registran los argumentos completos —la query PromQL tal cual salió— pero
**no el resultado**: el resultado son los datos del cliente, y guardarlos acá
sería hacer una copia de sus métricas en un buffer que nadie pidió. Queda el
tamaño y si salió bien, que es lo que hace falta para auditar.

Buffer en memoria y anillo: F0 no tiene base de datos y no la va a inventar
para esto. Persistir es de F7, junto con el resto de lo que hace falta para que
esto sea instalable. Que sea en memoria está dicho en `/v1/audit` para que nadie
lo confunda con un registro durable.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any

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
        "durable": False,
    }


def reset() -> None:
    with _lock:
        _entries.clear()
