"""Lo que se le agrega a una alerta antes de entregarla.

Todo lo de acá es lo que Alertmanager no puede hacer porque no tiene con qué:
la expresión de la regla (la busca en el backend por nombre), cómo venía esa
expresión antes del disparo, cuántas otras alertas hay activas, si el gasto
está raro, y —si hay modelo— una explicación corta con hipótesis y qué mirar.

Cada pieza es independiente y falla sola: si el backend no devuelve la regla,
la alerta sale igual, con menos. Una alerta que no llega porque el
enriquecimiento explotó es peor que una alerta pelada.

El triage con el modelo es lo caro: un turno del agente por alerta, con sus
tools y su presupuesto. Por eso corre después de que el despachante decidió
que el aviso sale (una alerta deduplicada no gasta tokens) y bajo un
semáforo. Y por eso el prompt le pide poco: seis líneas, no un informe.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..agent import registry
from ..agent.tools_metrics import _summarize
from ..ports.alerts import Signal
from ..ports.metrics import Rule

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnrichConfig:
    triage: bool = True
    lookback: timedelta = timedelta(hours=1)
    max_concurrent: int = 2
    queue_max: int = 200


@dataclass
class Enrichment:
    expression: str = ""
    duration_s: float = 0.0
    trend: list[dict[str, Any]] = field(default_factory=list)
    active_alerts: int | None = None
    cost: dict[str, Any] | None = None
    triage: str = ""
    triage_tools: list[str] = field(default_factory=list)
    tokens: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "expression": self.expression, "duration_s": self.duration_s,
            "trend": self.trend, "active_alerts": self.active_alerts, "cost": self.cost,
            "triage": self.triage, "triage_tools": self.triage_tools,
            "tokens": self.tokens, "errors": self.errors,
        }


async def rules_by_name(rt: Any) -> dict[str, Rule]:
    """Una vez por lote, no una por alerta."""
    if rt.metrics is None:
        return {}
    try:
        return {r.name: r for r in await rt.metrics.rules()}
    except Exception as e:
        logger.info("enrich: sin reglas del backend (%s)", e)
        return {}


async def enrich(rt: Any, s: Signal, cfg: EnrichConfig, *,
                 rules: dict[str, Rule] | None = None) -> Enrichment:
    e = Enrichment()
    regla = (rules or {}).get(s.name)
    e.expression = s.expression or (regla.expression if regla else "")
    e.duration_s = regla.duration_s if regla else 0.0

    if rt.metrics is not None:
        await asyncio.gather(_trend(rt, s, e, cfg), _active(rt, e))
    if rt.cost is not None:
        await _cost(rt, e)
    if cfg.triage and rt.model is not None:
        await _triage(rt, s, e)
    return e


async def _trend(rt: Any, s: Signal, e: Enrichment, cfg: EnrichConfig) -> None:
    if not e.expression:
        return
    try:
        fin = datetime.now(UTC)
        r = await rt.metrics.range(e.expression, start=s.starts_at - cfg.lookback, end=fin)
        # Sólo las series de ESTA alerta cuando se puede distinguir: una regla
        # que dispara sobre 40 instancias devuelve 40 series y la que importa
        # es la del labelset del disparo.
        propias = [x for x in r.series if all(x.labels.get(k) == v for k, v in s.labels.items()
                                              if k in x.labels and k != "alertname")]
        e.trend = [_summarize(x) for x in (propias or r.series)[:5]]
    except Exception as ex:
        e.errors.append(f"tendencia: {type(ex).__name__}: {str(ex)[:120]}")


async def _active(rt: Any, e: Enrichment) -> None:
    try:
        e.active_alerts = sum(1 for a in await rt.metrics.alerts() if a.state == "firing")
    except Exception as ex:
        e.errors.append(f"activas: {type(ex).__name__}: {str(ex)[:120]}")


async def _cost(rt: Any, e: Enrichment) -> None:
    r = await registry.execute("cost_daily", {"days": 14}, rt.context())
    if r.get("status") != "ok" or not r["result"].get("points"):
        e.errors.append(f"costo: {str(r.get('result'))[:120]}")
        return
    res = r["result"]
    e.cost = {"last_day": res["last_day"], "median_day": res["median_day"],
              "last_vs_median": res["last_vs_median"], "currency": res["currency"]}


def _triage_question(s: Signal, e: Enrichment) -> str:
    labels = ", ".join(f'{k}="{v}"' for k, v in sorted(s.labels.items()) if k != "alertname")
    partes = [
        f"Llegó esta alerta de Alertmanager: {s.name} (severity={s.severity}), "
        f"disparada a las {s.starts_at.isoformat(timespec='minutes')} con labels {{{labels}}}.",
    ]
    if s.summary and s.summary != s.name:
        partes.append(f"Resumen de la regla: {s.summary}")
    if e.expression:
        partes.append(f"La expresión que la dispara es: {e.expression}")
    if e.trend:
        partes.append(f"Su tendencia en la última hora, ya consultada: {e.trend}")
    if e.active_alerts is not None:
        partes.append(f"Hay {e.active_alerts} alerta(s) firing en total en el backend.")
    if e.cost:
        partes.append(f"Gasto: el último día fue {e.cost['last_vs_median']}× la mediana.")
    partes.append(
        "Hacé el triage en no más de seis líneas: qué está pasando en concreto (el "
        "recurso, el número), la hipótesis más probable de la causa, y las dos "
        "cosas que hay que mirar primero. Usá tools sólo si te falta un dato que "
        "cambie el diagnóstico. Sin preámbulos.")
    return "\n".join(partes)


async def _triage(rt: Any, s: Signal, e: Enrichment) -> None:
    from .. import service  # acá y no arriba: service → runtime → config → este módulo

    try:
        r = await service.answer(rt, _triage_question(s, e), actor="alertmanager")
        e.triage = r.text.strip()
        e.triage_tools = r.tools_used
        e.tokens = r.tokens
    except Exception as ex:
        e.errors.append(f"triage: {type(ex).__name__}: {str(ex)[:120]}")
