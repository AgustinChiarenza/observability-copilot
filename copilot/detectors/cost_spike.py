"""Detector de pico de gasto.

La regla es la misma que devuelve `cost_daily` al agente, para que lo que
dispara el sistema y lo que el modelo le explica al usuario coincidan:

    último día completo / mediana de la ventana  >=  threshold

Mediana, no promedio: un pico anterior dentro de la ventana infla el promedio y
tapa el siguiente. Y "último día completo" quiere decir que se respeta el
`lag_days` del puerto de costos — el día que todavía se está llenando siempre
parece una caída del 60% (o, si el exporter acumula, un pico), y es la falsa
alarma más común de FinOps.

`min_amount` es el piso absoluto: pasar de 2 a 6 dólares es 3× y no le importa
a nadie. Sin piso, una cuenta chica dispara todos los días.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import telemetry
from ..dispatch import Outcome
from ..ports.cost import CostPort, CostSlice
from ..ports.notify import Message, Severity

logger = logging.getLogger(__name__)

NAME = "cost_spike"


@dataclass(frozen=True)
class SpikeConfig:
    enabled: bool = False
    interval: timedelta = timedelta(hours=1)
    window_days: int = 14
    #: Cociente último día / mediana a partir del cual es pico.
    threshold: float = 1.5
    #: Por debajo de este monto diario no se avisa, sea cual sea el cociente.
    min_amount: float = 0.0
    #: Con menos días con dato, no hay baseline y no se opina.
    min_days: int = 5


@dataclass(frozen=True)
class Finding:
    day: str
    amount: float
    median: float
    ratio: float
    currency: str
    window_days: int
    top_services: list[CostSlice] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day, "amount": round(self.amount, 2),
            "median": round(self.median, 2), "ratio": round(self.ratio, 2),
            "currency": self.currency, "window_days": self.window_days,
            "top_services": [{"service": s.key, "amount": round(s.amount, 2)}
                             for s in self.top_services],
        }


@dataclass
class Verdict:
    """Resultado de una evaluación: `outcome` es spike | clear | no_data."""

    outcome: str
    detail: str = ""
    finding: Finding | None = None
    points: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": NAME, "outcome": self.outcome, "detail": self.detail,
            "points": self.points,
            "finding": self.finding.as_dict() if self.finding else None,
        }


async def evaluate(cost: CostPort, cfg: SpikeConfig) -> Verdict:
    hoy = datetime.now(UTC).date()
    hasta = hoy - timedelta(days=max(0, cost.lag_days))
    desde = hasta - timedelta(days=max(1, cfg.window_days) - 1)
    puntos = [p for p in await cost.daily_series(start=desde, end=hasta) if p.amount > 0]

    if len(puntos) < cfg.min_days:
        return Verdict("no_data", f"{len(puntos)} día(s) con dato; hacen falta "
                                  f"{cfg.min_days} para tener baseline.", points=len(puntos))

    ultimo = puntos[-1]
    # La mediana se calcula SIN el último día: es el baseline contra el que se
    # lo compara, y meterlo adentro lo acerca a sí mismo.
    base = [p.amount for p in puntos[:-1]]
    mediana = statistics.median(base)
    if mediana <= 0:
        return Verdict("no_data", "la mediana del período es cero.", points=len(puntos))
    ratio = ultimo.amount / mediana

    if ratio < cfg.threshold or ultimo.amount < cfg.min_amount:
        return Verdict(
            "clear",
            f"{ultimo.day.isoformat()}: {ultimo.amount:.2f} {ultimo.currency} es "
            f"{ratio:.2f}× la mediana ({mediana:.2f}); umbral {cfg.threshold}×.",
            points=len(puntos))

    servicios: list[CostSlice] = []
    try:
        servicios = await cost.by_service(start=ultimo.day, end=ultimo.day, limit=5)
    except Exception as e:
        # El desglose es el "por qué"; sin él el aviso sale igual, más pobre.
        logger.info("cost_spike: sin desglose por servicio (%s)", e)

    f = Finding(
        day=ultimo.day.isoformat(), amount=ultimo.amount, median=mediana, ratio=ratio,
        currency=ultimo.currency, window_days=cfg.window_days, top_services=servicios,
    )
    return Verdict("spike", f"{f.day}: {ratio:.2f}× la mediana.", finding=f,
                   points=len(puntos))


def to_message(f: Finding) -> Message:
    """El texto del aviso. Corto arriba, porque un SMS se queda con el título y
    una línea; el desglose abajo para los canales que tienen lugar."""
    lineas = [
        f"El {f.day} se gastaron {f.amount:,.2f} {f.currency}: {f.ratio:.1f}× la "
        f"mediana de los últimos {f.window_days} días ({f.median:,.2f}).",
    ]
    if f.top_services:
        lineas.append("Ese día, lo que más pesó:")
        lineas += [f"  · {s.key}: {s.amount:,.2f} {s.currency}" for s in f.top_services]
    return Message(
        title=f"Pico de gasto: {f.ratio:.1f}× el día {f.day}",
        body="\n".join(lineas),
        severity=Severity.CRITICAL if f.ratio >= 3 else Severity.WARNING,
        # Un pico es de un día. Si mañana hay otro, es otro aviso — y el de
        # hoy no se repite: el detector corre cada hora y ese día sigue siendo
        # "el último completo" hasta mañana, así que con el repeat_interval
        # general saldrían seis avisos iguales.
        fingerprint=f"{NAME}:{f.day}",
        labels={"detector": NAME, "day": f.day},
        repeat_after=timedelta(days=3),
    )


async def run_once(rt: Any) -> dict[str, Any]:
    """Evalúa y, si hay pico, lo despacha. Devuelve el veredicto y qué pasó con
    el aviso. Lo llaman el scheduler, la API y el CLI: una sola implementación."""
    cfg: SpikeConfig = rt.config.detectors.cost_spike
    if rt.cost is None:
        return Verdict("no_data", "no hay CostPort configurado.").as_dict()
    try:
        v = await evaluate(rt.cost, cfg)
    except Exception as e:
        telemetry.DETECTOR_RUNS.labels(detector=NAME, outcome="error").inc()
        logger.warning("cost_spike: falló la evaluación: %s: %s", type(e).__name__, e)
        return Verdict("error", f"{type(e).__name__}: {e}").as_dict()

    telemetry.DETECTOR_RUNS.labels(detector=NAME, outcome=v.outcome).inc()
    salida = v.as_dict()
    if v.finding is not None:
        out: Outcome = await rt.dispatcher.send(to_message(v.finding))
        salida["notify"] = out.as_dict()
        logger.warning("cost_spike: %s → %s", v.detail, out.decision)
    else:
        logger.info("cost_spike: %s — %s", v.outcome, v.detail)
    return salida
