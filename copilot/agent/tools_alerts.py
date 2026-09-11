"""Las tools de alertas: qué está en rojo y por qué regla.

Sin esto el agente sólo ve métricas, y "¿qué está disparando ahora?" —la primera
pregunta de cualquier guardia— no se puede contestar. Son lectura pura sobre el
mismo backend de métricas: no piden credenciales nuevas ni conocen a
Alertmanager.

Lo que devuelven es lo que **evalúa** el backend, no lo que **suena**: acá no
hay silences ni inhibición. Se dice en la descripción de la tool para que el
modelo no afirme "no hay nada silenciado" con un dato que no lo sabe.

La expresión de la regla es la parte que vale: es la query que hay que correr
en rango para explicar el disparo, y sale de acá en vez de adivinarse.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .registry import Context, tool
from .tools_metrics import _need_metrics

#: Alertas o reglas que se listan antes de pasar a "y otras N".
_MAX_LISTED = 100


def _sin_alertname(labels: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in labels.items() if k != "alertname"}


@tool(
    "alerts_active",
    "Alertas firing o pending según el evaluador de reglas del sistema de "
    "monitoreo. Para 'qué está disparando', 'hay algo en rojo', 'desde cuándo'. "
    "OJO: es lo que el backend evalúa, no lo que llega a la gente: no ve "
    "silences ni inhibición de Alertmanager.",
    {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": ["firing", "pending", "all"],
                "description": "Qué estado listar. Por defecto, sólo firing.",
                "default": "firing",
            },
            "contains": {
                "type": "string",
                "description": "Filtra por subcadena en el nombre de la alerta.",
            },
        },
    },
)
async def alerts_active(ctx: Context, state: str = "firing", contains: str = "") -> dict[str, Any]:
    todas = await _need_metrics(ctx).alerts()
    firing = sum(1 for a in todas if a.state == "firing")
    listado = todas if state == "all" else [a for a in todas if a.state == state]
    if contains:
        aguja = contains.lower()
        listado = [a for a in listado if aguja in a.name.lower()]

    por_nombre: dict[str, int] = {}
    for a in listado:
        por_nombre[a.name] = por_nombre.get(a.name, 0) + 1

    salida: dict[str, Any] = {
        "total": len(todas),
        "firing": firing,
        "pending": len(todas) - firing,
        "shown": len(listado),
        "by_name": por_nombre,
        "alerts": [
            {
                "name": a.name,
                "state": a.state,
                **({"severity": a.severity} if a.severity else {}),
                "labels": _sin_alertname(a.labels),
                **({"active_at": a.active_at.isoformat()} if a.active_at else {}),
                **({"value": a.value} if a.value else {}),
                **({"summary": a.annotations["summary"]} if a.annotations.get("summary") else {}),
                **({"description": a.annotations["description"][:300]}
                   if a.annotations.get("description") else {}),
            }
            for a in listado[:_MAX_LISTED]
        ],
    }
    if len(listado) > _MAX_LISTED:
        salida["note"] = (
            f"Se listan {_MAX_LISTED} de {len(listado)}. Acotá con `contains` o "
            f"mirá `by_name` para ver cuáles se repiten.")
    return salida


@tool(
    "alert_rules",
    "Reglas de alerta configuradas, con su expresión PromQL, su `for` y su "
    "estado. Para 'qué alertas existen', 'cuál es el umbral de X', y para "
    "sacar la expresión que hay que correr con promql_range para explicar "
    "un disparo.",
    {
        "type": "object",
        "properties": {
            "contains": {
                "type": "string",
                "description": "Filtra por subcadena en el nombre de la regla.",
            },
            "only_active": {
                "type": "boolean",
                "description": "Sólo las que están firing o pending.",
                "default": False,
            },
        },
    },
)
async def alert_rules(ctx: Context, contains: str = "", only_active: bool = False) -> dict[str, Any]:
    todas = await _need_metrics(ctx).rules()
    listado = todas
    if only_active:
        listado = [r for r in listado if r.state != "inactive"]
    if contains:
        aguja = contains.lower()
        listado = [r for r in listado if aguja in r.name.lower()]

    salida: dict[str, Any] = {
        "total": len(todas),
        "shown": len(listado),
        "rules": [
            {
                "name": r.name,
                "group": r.group,
                "state": r.state,
                "expression": r.expression,
                "for_seconds": r.duration_s,
                **({"severity": r.labels["severity"]} if r.labels.get("severity") else {}),
                **({"active": r.active} if r.active else {}),
                # Una regla con `health: err` no dispara aunque el sistema esté
                # en llamas: es la alerta que falta, y hay que decirlo.
                **({"health": r.health, "last_error": r.last_error[:200]}
                   if r.health == "err" else {}),
            }
            for r in listado[:_MAX_LISTED]
        ],
    }
    if len(listado) > _MAX_LISTED:
        salida["note"] = f"Se listan {_MAX_LISTED} de {len(listado)}. Acotá con `contains`."
    return salida


@tool(
    "alerts_received",
    "Alertas que Alertmanager le mandó a este copiloto (ya pasadas por sus "
    "silences e inhibición), con el triage que se hizo de cada una. Para "
    "'qué pasó anoche', 'qué llegó en las últimas horas', 'qué se dijo de X'. "
    "Es lo que SONÓ, a diferencia de alerts_active que es lo que el backend evalúa.",
    {
        "type": "object",
        "properties": {
            "hours": {"type": "number", "description": "Cuántas horas hacia atrás.", "default": 24},
            "only_firing": {"type": "boolean", "default": False},
            "contains": {"type": "string", "description": "Filtra por nombre."},
        },
    },
)
async def alerts_received(ctx: Context, hours: float = 24, only_firing: bool = False,
                          contains: str = "") -> dict[str, Any]:
    log = ctx.extras.get("alert_log")
    if log is None:
        raise RuntimeError("Esta instalación no recibe alertas de Alertmanager.")
    desde = datetime.now(UTC) - timedelta(hours=max(0.1, hours))
    filas = log.recent(limit=_MAX_LISTED, only_firing=only_firing, since=desde)
    if contains:
        aguja = contains.lower()
        filas = [r for r in filas if aguja in r.signal.name.lower()]
    return {
        "hours": hours,
        "count": len(filas),
        "alerts": [
            {
                "name": r.signal.name, "status": str(r.signal.status),
                "severity": r.signal.severity,
                "labels": {k: v for k, v in r.signal.labels.items()
                           if k not in ("alertname", "severity")},
                "starts_at": r.signal.starts_at.isoformat(timespec="minutes"),
                "received_at": r.received_at[:16],
                "decision": r.decision,
                **({"triage": r.enrichment["triage"]}
                   if r.enrichment and r.enrichment.get("triage") else {}),
            }
            for r in filas
        ],
    }
