"""CostPort: leer el costo de donde ya está.

El orden de los adapters importa y es contraintuitivo. El primero **no** es el
SDK de facturación: es `cost_promql`, que lee el costo de la misma TSDB donde ya
están las métricas. Muchos clientes ya exportan su gasto ahí (kubecost,
opencost, un exporter propio de billing, un recording rule sobre la factura), y
en ese caso pedirle credenciales de facturación a alguien para ir a buscar un
dato que ya tiene en casa es pedir permisos de más para llegar más tarde y peor:
BSS tarda y tiene rate limit, la TSDB responde en milisegundos.

El adapter que va contra la API de facturación existe para cuando el dato NO
está — no como camino principal.

Y acá se termina lo genérico. Huawei BSS, AWS CUR, Azure Cost Management y el
export a BigQuery de GCP no comparten granularidad, ni latencia, ni modelo de
identidad. Este puerto es la línea donde el producto deja de ser universal y
pasa a ser una colección de adapters — vale la pena decirlo en voz alta antes de
que alguien lo prometa en una reunión.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable


class CostError(RuntimeError):
    """El backend de costos no pudo responder."""

    def __init__(self, msg: str, *, retriable: bool = False):
        super().__init__(msg)
        self.retriable = retriable


@dataclass(frozen=True)
class CostPoint:
    """El gasto de un día. Un día, no una hora: ninguna nube factura con
    granularidad menor de forma confiable, y fingir que sí produce picos que son
    artefactos del reloj de la nube y no del consumo."""

    day: date
    amount: float
    currency: str = "USD"


@dataclass(frozen=True)
class CostSlice:
    """Gasto agrupado por algo (servicio, recurso, proyecto, tag)."""

    key: str
    amount: float
    currency: str = "USD"
    labels: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class CostPort(Protocol):
    """Sólo lectura. Nada acá crea, mueve ni cancela un recurso facturable."""

    name: str

    #: Con cuánto atraso llega el dato. BSS puede ir un día atrás; una TSDB va
    #: al minuto. El detector de picos lo necesita para no gritar por un día
    #: que todavía se está llenando — la falsa alarma más común de FinOps.
    lag_days: int

    async def daily_series(self, *, start: date, end: date) -> list[CostPoint]:
        """Gasto por día. La base del detector de picos."""
        ...

    async def by_service(self, *, start: date, end: date, limit: int = 10) -> list[CostSlice]:
        """Gasto por servicio en el período, de mayor a menor. Es lo que
        convierte "gastaste 3× de más" en algo accionable."""
        ...

    async def by_resource(
        self, *, start: date, end: date, limit: int = 20, service: str | None = None,
    ) -> list[CostSlice]:
        """Gasto por recurso individual."""
        ...

    async def check(self) -> None:
        """Ping. Levanta `CostError` si el backend no está usable."""
        ...
