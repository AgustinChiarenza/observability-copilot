"""Las tools de costo. Sólo existen si hay un CostPort configurado.

`cost_daily` devuelve además la mediana de la ventana y el cociente del último
día contra ella. No es un cálculo que el modelo no pueda hacer: es que si se lo
dejás a él, a veces lo hace bien y a veces promedia mal, y la diferencia entre
promedio y mediana es justo la que decide si un pico previo se come el baseline.
Un número que dispara alarmas se calcula en código, se testea, y se le entrega
hecho.

Por lo mismo se recorta la ventana con `lag_days`: el día que todavía se está
llenando siempre parece una caída del 60%. Es la falsa alarma más común de
FinOps y sale gratis no cometerla.
"""
from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from ..ports.cost import CostPort
from .registry import Context, tool


def _need_cost(ctx: Context) -> CostPort:
    if ctx.cost is None:
        raise RuntimeError(
            "Esta instalación no tiene backend de costos configurado, así que no "
            "puedo responder sobre gasto. Se configura en el bloque `cost` del YAML.")
    return ctx.cost


#: Techo de la ventana. Contra BSS, cada mes son hasta 20.000 records: un
#: `days: 3650` que al modelo se le ocurra no puede convertirse en 120 meses
#: de facturación bajados de una.
MAX_DAYS = 92


def _window(days: int, lag_days: int) -> tuple[Any, Any]:
    hoy = datetime.now(UTC).date()
    fin = hoy - timedelta(days=max(0, lag_days))
    return fin - timedelta(days=min(max(1, days), MAX_DAYS) - 1), fin


@tool(
    "cost_daily",
    "Gasto diario de los últimos N días, con la mediana del período y cuánto "
    "se desvía el último día. Para 'cuánto estoy gastando', 'subió el gasto', "
    "'hubo un pico'.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Días hacia atrás.", "default": 14},
        },
    },
)
async def cost_daily(ctx: Context, days: int = 14) -> dict[str, Any]:
    port = _need_cost(ctx)
    desde, hasta = _window(days, port.lag_days)
    puntos = await port.daily_series(start=desde, end=hasta)
    if not puntos:
        return {"days": days, "points": [], "note": "El backend no devolvió datos de costo."}

    montos = [p.amount for p in puntos]
    ultimo = puntos[-1]
    # La mediana SIN el último día, igual que el detector de picos: es el
    # baseline contra el que se compara, y meterlo adentro lo acerca a sí
    # mismo. Si los dos números no coincidieran, el modelo explicaría un
    # cociente distinto del que disparó la alarma.
    mediana = statistics.median(montos[:-1] if len(montos) > 1 else montos)
    return {
        "from": desde.isoformat(),
        "to": hasta.isoformat(),
        "currency": puntos[0].currency,
        "lag_days": port.lag_days,
        "total": round(sum(montos), 2),
        "median_day": round(mediana, 2),
        "last_day": {"day": ultimo.day.isoformat(), "amount": round(ultimo.amount, 2)},
        # El cociente contra la mediana es el número que usa el detector de
        # picos; el modelo lo recibe hecho para que su lectura coincida con la
        # alerta que dispararía el sistema.
        "last_vs_median": round(ultimo.amount / mediana, 2) if mediana else None,
        "points": [[p.day.isoformat(), round(p.amount, 2)] for p in puntos],
    }


@tool(
    "cost_by_service",
    "Qué servicios se llevan el gasto en un período, de mayor a menor. Es lo "
    "que convierte 'gastaste de más' en algo accionable.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Días hacia atrás.", "default": 7},
            "limit": {"type": "integer", "description": "Cuántos devolver.", "default": 10},
        },
    },
)
async def cost_by_service(ctx: Context, days: int = 7, limit: int = 10) -> dict[str, Any]:
    port = _need_cost(ctx)
    desde, hasta = _window(days, port.lag_days)
    filas = await port.by_service(start=desde, end=hasta, limit=limit)
    total = sum(f.amount for f in filas)
    return {
        "from": desde.isoformat(),
        "to": hasta.isoformat(),
        "total_shown": round(total, 2),
        "services": [
            {"service": f.key, "amount": round(f.amount, 2), "currency": f.currency,
             "share_pct": round(100 * f.amount / total, 1) if total else None}
            for f in filas
        ],
    }


@tool(
    "cost_by_resource",
    "Los recursos individuales más caros de un período. Para bajar del "
    "servicio al recurso concreto que hay que mirar.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Días hacia atrás.", "default": 7},
            "limit": {"type": "integer", "description": "Cuántos devolver.", "default": 20},
            "service": {"type": "string", "description": "Acotar a un servicio."},
        },
    },
)
async def cost_by_resource(
    ctx: Context, days: int = 7, limit: int = 20, service: str = "",
) -> dict[str, Any]:
    port = _need_cost(ctx)
    desde, hasta = _window(days, port.lag_days)
    filas = await port.by_resource(start=desde, end=hasta, limit=limit,
                                   service=service or None)
    return {
        "from": desde.isoformat(),
        "to": hasta.isoformat(),
        **({"service": service} if service else {}),
        "resources": [
            {"resource": f.key, "amount": round(f.amount, 2), "currency": f.currency,
             **({"labels": f.labels} if f.labels else {})}
            for f in filas
        ],
    }
