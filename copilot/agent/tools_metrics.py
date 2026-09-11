"""Las tools de lectura sobre el TSDB del cliente.

Lo que más define la calidad del agente acá no es qué tools hay, es **cómo
vuelve el resultado**. Un `query_range` de 200 series por 300 puntos son 60.000
números: entran al contexto, lo llenan, y el modelo termina razonando sobre
menos información útil que si le hubieras dado un resumen. Por eso las series
vuelven resumidas —primero, último, mín, máx, promedio— y los puntos crudos sólo
cuando son pocos.

El otro criterio: los rangos se piden en criollo (`lookback: "6h"`), no con
timestamps. Pedirle al modelo que calcule epochs es pedirle que se equivoque en
la zona horaria, y equivocarse ahí no da un error: da un gráfico plano de un
período vacío que parece un dato.
"""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from ..ports.metrics import Range, Series
from .registry import Context, tool

_RE_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d|w)\s*$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

#: Por encima de esto, la serie vuelve resumida en vez de punto por punto.
_MAX_RAW_POINTS = 12
#: Cuántas series se detallan antes de pasar a "y otras N".
_MAX_DETAILED = 25


def _lookback(raw: str) -> timedelta:
    m = _RE_DUR.match(str(raw))
    if not m:
        raise ValueError(
            f"'{raw}' no es una ventana válida. Usá un número con unidad: "
            f"'30m', '6h', '7d'.")
    return timedelta(seconds=float(m.group(1)) * _UNIT[m.group(2)])


def _num(v: float) -> float | None:
    """NaN e Inf no son JSON válido y rompen el serializador del turno."""
    return None if (math.isnan(v) or math.isinf(v)) else round(v, 6)


def _label_str(labels: dict[str, str]) -> str:
    utiles = {k: v for k, v in labels.items() if k != "__name__"}
    if not utiles:
        return labels.get("__name__", "{}")
    cuerpo = ", ".join(f'{k}="{v}"' for k, v in sorted(utiles.items()))
    return f"{labels.get('__name__', '')}{{{cuerpo}}}"


def _summarize(s: Series) -> dict[str, Any]:
    valores = [p.value for p in s.samples if not math.isnan(p.value)]
    fila: dict[str, Any] = {"series": _label_str(s.labels)}
    if not valores:
        fila["value"] = None
        return fila
    if len(s.samples) <= _MAX_RAW_POINTS:
        fila["points"] = [[p.ts.isoformat(), _num(p.value)] for p in s.samples]
        if len(s.samples) == 1:
            fila["value"] = _num(s.samples[0].value)
        return fila
    fila.update({
        "first": _num(valores[0]),
        "last": _num(valores[-1]),
        "min": _num(min(valores)),
        "max": _num(max(valores)),
        "avg": _num(sum(valores) / len(valores)),
        "points": len(valores),
    })
    return fila


def _render(series: list[Series], truncated: bool, **extra: Any) -> dict[str, Any]:
    detalle = [_summarize(s) for s in series[:_MAX_DETAILED]]
    salida: dict[str, Any] = {"count": len(series), "series": detalle, **extra}
    if len(series) > _MAX_DETAILED:
        salida["note"] = (
            f"Se detallan {_MAX_DETAILED} de {len(series)} series. Agregá la query "
            f"con sum by (...) o topk(...) para ver el resto.")
    if truncated:
        # Silenciar esto sería devolver una parte como si fuera el todo, que es
        # la forma más cara de equivocarse: nadie la ve hasta que la conclusión
        # ya está tomada.
        salida["truncated"] = True
        salida["warning"] = (
            "El backend devolvió más series que el tope configurado y el resto "
            "se descartó. Este resultado es PARCIAL.")
    return salida


def _need_metrics(ctx: Context):
    if ctx.metrics is None:
        raise RuntimeError(
            "No hay backend de métricas configurado en esta instalación.")
    return ctx.metrics


@tool(
    "promql_instant",
    "Valor actual de una expresión PromQL contra el sistema de monitoreo del "
    "cliente. Para 'cuánto vale ahora'. Ejemplos: 'up', "
    "'sum by (job) (up == 0)', 'topk(5, node_load1)'.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Expresión PromQL."},
        },
        "required": ["query"],
    },
)
async def promql_instant(ctx: Context, query: str) -> dict[str, Any]:
    r = await _need_metrics(ctx).instant(query)
    return _render(r.series, r.truncated, query=r.query, at=r.at.isoformat())


@tool(
    "promql_range",
    "Evolución de una expresión PromQL en una ventana hacia atrás. Para "
    "'cómo viene', 'subió o bajó', 'cuándo empezó'. Devuelve un resumen "
    "(primero, último, mín, máx, promedio) por serie.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Expresión PromQL."},
            "lookback": {
                "type": "string",
                "description": "Ventana hacia atrás desde ahora: '30m', '6h', '7d'.",
                "default": "1h",
            },
        },
        "required": ["query"],
    },
)
async def promql_range(ctx: Context, query: str, lookback: str = "1h") -> dict[str, Any]:
    ventana = _lookback(lookback)
    fin = datetime.now(UTC)
    r: Range = await _need_metrics(ctx).range(query, start=fin - ventana, end=fin)
    return _render(
        r.series, r.truncated,
        query=r.query, lookback=lookback,
        start=r.start.isoformat(), end=r.end.isoformat(), step_seconds=r.step_s,
    )


@tool(
    "label_values",
    "Valores que toma un label. Es la forma de descubrir qué hay antes de "
    "escribir una query: usá label='__name__' para listar métricas "
    "disponibles, o label='job' para ver qué se está monitoreando.",
    {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Nombre del label. '__name__' lista métricas."},
            "matches": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Selectores opcionales para acotar, ej: ['{job=\"node\"}'].",
            },
            "contains": {
                "type": "string",
                "description": "Filtra los valores devueltos por subcadena. Usalo "
                               "con __name__ para buscar métricas por tema.",
            },
        },
        "required": ["label"],
    },
)
async def label_values(
    ctx: Context, label: str, matches: list[str] | None = None, contains: str = "",
) -> dict[str, Any]:
    valores = await _need_metrics(ctx).label_values(label, matches=matches)
    if contains:
        aguja = contains.lower()
        valores = [v for v in valores if aguja in v.lower()]
    return {"label": label, "count": len(valores), "values": valores[:200]}


@tool(
    "targets_health",
    "Estado de los targets de scrape: cuántos están arriba, cuántos caídos y "
    "con qué error. Para 'está todo monitoreado', 'qué dejó de reportar'.",
    {
        "type": "object",
        "properties": {
            "only_down": {
                "type": "boolean",
                "description": "Devolver solamente los caídos.",
                "default": False,
            },
        },
    },
)
async def targets_health(ctx: Context, only_down: bool = False) -> dict[str, Any]:
    todos = await _need_metrics(ctx).targets()
    arriba = sum(1 for t in todos if t.up)
    caidos = [t for t in todos if not t.up]
    listado = caidos if only_down else todos
    return {
        "total": len(todos),
        "up": arriba,
        "down": len(caidos),
        "targets": [
            {"job": t.job, "instance": t.instance, "health": t.health,
             **({"last_error": t.last_error[:200]} if t.last_error else {})}
            for t in listado[:100]
        ],
    }


@tool(
    "metric_metadata",
    "Tipo (counter, gauge, histogram, summary), unidad y descripción de las "
    "métricas cuyo nombre contiene un texto. Para saber qué es una métrica "
    "antes de usarla: a un counter se le hace rate(), a un gauge no.",
    {
        "type": "object",
        "properties": {
            "contains": {
                "type": "string",
                "description": "Substring del nombre. Vacío lista las primeras.",
                "default": "",
            },
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
        },
    },
)
async def metric_metadata(ctx: Context, contains: str = "", limit: int = 50) -> dict[str, Any]:
    limite = max(1, min(int(limit), 200))
    fichas = await _need_metrics(ctx).metadata(contains=contains, limit=limite + 1)
    return {
        "count": min(len(fichas), limite),
        "truncated": len(fichas) > limite,
        "metrics": [
            {"name": m.name, "type": m.type,
             **({"unit": m.unit} if m.unit else {}),
             **({"help": m.help} if m.help else {})}
            for m in fichas[:limite]
        ],
    }
