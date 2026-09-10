"""El costo, leído de la misma TSDB donde ya están las métricas.

Este es el adapter de costos por defecto, y el orden no es casual. Si el cliente
ya tiene su gasto en Prometheus —kubecost, opencost, un exporter de billing, un
recording rule sobre la factura— entonces el dato ya está en casa: responde en
milisegundos, no necesita credenciales nuevas y no tiene rate limit. Ir a
buscarlo a la API de facturación en ese escenario es pedir más permisos para
llegar más tarde y peor.

Y es genérico de verdad, porque no sabe nada de ninguna nube: el cliente escribe
sus propias expresiones en el YAML y este adapter las corre. Sirve igual con
métricas de Huawei, de AWS o de un CSV que alguien convirtió en gauge.

    cost:
      adapter: promql
      daily: 'sum(increase(cloud_cost_usd_total[1d]))'
      by_service: 'sum by (service) (increase(cloud_cost_usd_total[1d]))'
      by_resource: 'sum by (resource_id) (increase(cloud_cost_usd_total[1d]))'
      currency: USD
      lag_days: 0
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from ..ports.cost import CostError, CostPoint, CostSlice
from ..ports.metrics import MetricsError, MetricsPort
from . import register

logger = logging.getLogger(__name__)


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    inicio = datetime.combine(d, time.min, tzinfo=UTC)
    return inicio, inicio + timedelta(days=1)


@register("cost", "promql")
class PromqlCost:
    """Costo sobre expresiones PromQL que define el cliente."""

    def __init__(
        self,
        *,
        metrics: MetricsPort,
        daily: str = "",
        by_service: str = "",
        by_resource: str = "",
        currency: str = "USD",
        lag_days: int = 0,
        name: str = "promql",
        **_ignored: Any,
    ):
        if not daily:
            raise ValueError(
                "cost.daily: falta la expresión del gasto diario. Es la única "
                "obligatoria — sin ella no hay detección de picos.")
        self.name = name
        self.lag_days = int(lag_days)
        self.currency = currency
        self._m = metrics
        self._daily = daily
        self._by_service = by_service
        self._by_resource = by_resource

    async def daily_series(self, *, start: date, end: date) -> list[CostPoint]:
        """Un punto por día.

        El step se fija en 86400 a mano en vez de dejar que lo elija el adapter
        de métricas: un step de 6 horas sobre un `increase(...[1d])` devuelve
        cuatro puntos por día que se pisan, y la mediana del baseline se calcula
        sobre ventanas solapadas. El resultado es un baseline inflado y picos
        que no existen.
        """
        desde, _ = _day_bounds(start)
        _, hasta = _day_bounds(end)
        try:
            r = await self._m.range(self._daily, start=desde, end=hasta, step_s=86400.0)
        except MetricsError as e:
            raise CostError(f"{self.name}: {e}", retriable=e.retriable) from e

        if not r.series:
            return []
        # Se espera una sola serie (un agregado). Si el cliente escribió una
        # expresión que devuelve varias, sumarlas por timestamp es lo correcto
        # y además lo que él esperaba.
        por_dia: dict[date, float] = {}
        for s in r.series:
            for punto in s.samples:
                if math.isnan(punto.value):
                    continue
                por_dia[punto.ts.date()] = por_dia.get(punto.ts.date(), 0.0) + punto.value
        return [CostPoint(day=d, amount=round(v, 4), currency=self.currency)
                for d, v in sorted(por_dia.items()) if start <= d <= end]

    async def _slices(self, expr: str, *, start: date, end: date, limit: int,
                      que: str) -> list[CostSlice]:
        if not expr:
            raise CostError(
                f"{self.name}: no hay expresión configurada para {que}. Agregá "
                f"`cost.{que}` al YAML si querés desglose por {que}.")
        desde, hasta = _day_bounds(start)[0], _day_bounds(end)[1]
        span_dias = max(1, (hasta - desde).days)
        # Instantánea al final del período, sobre la ventana completa: el
        # desglose es "cuánto salió cada cosa en estos N días", no una serie.
        query = expr.replace("[1d]", f"[{span_dias}d]")
        try:
            snap = await self._m.instant(query, at=hasta)
        except MetricsError as e:
            raise CostError(f"{self.name}: {e}", retriable=e.retriable) from e

        salida: list[CostSlice] = []
        for s in snap.series:
            ultimo = s.last
            if ultimo is None or math.isnan(ultimo.value):
                continue
            etiquetas = {k: v for k, v in s.labels.items() if k != "__name__"}
            # La llave es el único label que quedó tras el `by (...)`. Si hay
            # varios, se pegan: mejor "prod/ecs" que elegir uno en secreto.
            clave = "/".join(v for _, v in sorted(etiquetas.items())) or "(sin etiqueta)"
            salida.append(CostSlice(key=clave, amount=round(ultimo.value, 4),
                                    currency=self.currency, labels=etiquetas))
        salida.sort(key=lambda c: c.amount, reverse=True)
        return salida[:limit]

    async def by_service(self, *, start: date, end: date, limit: int = 10) -> list[CostSlice]:
        return await self._slices(self._by_service, start=start, end=end,
                                  limit=limit, que="by_service")

    async def by_resource(
        self, *, start: date, end: date, limit: int = 20, service: str | None = None,
    ) -> list[CostSlice]:
        filas = await self._slices(self._by_resource, start=start, end=end,
                                   limit=limit * 4 if service else limit, que="by_resource")
        if service:
            filas = [f for f in filas if service in f.labels.values()][:limit]
        return filas

    async def check(self) -> None:
        try:
            await self._m.instant(self._daily)
        except MetricsError as e:
            raise CostError(
                f"{self.name}: la expresión de cost.daily no corre contra el "
                f"backend de métricas ({e}).", retriable=e.retriable) from e
