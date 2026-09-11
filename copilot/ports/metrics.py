"""MetricsPort: leer las métricas que el cliente ya tiene.

Este es el puerto genérico de verdad. La HTTP API v1 de Prometheus es estándar
de hecho, así que un solo adapter cubre Prometheus, Thanos, Mimir,
VictoriaMetrics, Grafana Cloud y AMP. Por eso el puerto está modelado sobre ese
vocabulario (instant / range / series / labels) y no sobre una abstracción
propia: inventar un DSL intermedio sólo agregaría una traducción con pérdida
sobre un lenguaje que todos ya hablan.

**Todo lo que sale de acá está acotado.** No es una preferencia de diseño: una
query sin techo contra el Prometheus de producción de un cliente le hace daño a
él, y eso te saca de la cuenta. Los topes viven en `Budget` y el adapter los
aplica; el core nunca pide "todo".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable


class MetricsError(RuntimeError):
    """El backend de métricas no pudo responder.

    Lleva `retriable` porque el que llama necesita distinguir "tu PromQL está
    mal" (no reintentes nunca) de "el backend está saturado" (reintentá).
    """

    def __init__(self, msg: str, *, retriable: bool = False, query: str = ""):
        super().__init__(msg)
        self.retriable = retriable
        self.query = query


@dataclass(frozen=True)
class Budget:
    """Techo de una consulta. Lo aplica el adapter, no el que la pide."""

    max_series: int = 500
    max_points: int = 11_000        # el mismo tope que usa Prometheus por defecto
    max_range: timedelta = timedelta(days=31)
    timeout_s: float = 30.0


@dataclass(frozen=True)
class Sample:
    ts: datetime
    value: float


@dataclass(frozen=True)
class Series:
    """Una serie: sus labels y sus puntos.

    `labels` incluye `__name__` cuando el backend lo devuelve. No se filtra acá
    — el filtro de redacción es una decisión de política y vive en su propia
    capa, para que se pueda auditar en un solo lugar.
    """

    labels: dict[str, str]
    samples: list[Sample] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.labels.get("__name__", "")

    @property
    def last(self) -> Sample | None:
        return self.samples[-1] if self.samples else None


@dataclass(frozen=True)
class Instant:
    """Resultado de una query instantánea."""

    query: str
    at: datetime
    series: list[Series]
    truncated: bool = False   # se llegó a max_series y el resto se descartó

    def __len__(self) -> int:
        return len(self.series)


@dataclass(frozen=True)
class Range:
    """Resultado de una query de rango."""

    query: str
    start: datetime
    end: datetime
    step_s: float
    series: list[Series]
    truncated: bool = False


@dataclass(frozen=True)
class Alert:
    """Una alerta como la ve el evaluador de reglas (Prometheus, vmalert,
    Thanos/Mimir Ruler): el labelset, `state` firing o pending y desde cuándo.

    Es lo que dispara el backend, **antes** de Alertmanager: acá no se ven
    silences ni inhibición. Para "qué está sonando de verdad" hay que mirar el
    Alertmanager (F2); para "qué está evaluando en rojo" alcanza con esto.
    """

    name: str
    state: str                # "firing" | "pending"
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    active_at: datetime | None = None
    value: str = ""

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "")


@dataclass(frozen=True)
class Rule:
    """Una regla de alerta, con su expresión. La expresión vale oro: es la
    query que hay que correr en rango para explicar por qué disparó, sin tener
    que adivinarla."""

    name: str
    expression: str
    group: str = ""
    state: str = "inactive"   # "firing" | "pending" | "inactive"
    duration_s: float = 0.0   # el `for:`
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    health: str = "unknown"   # "ok" | "err" | "unknown"
    last_error: str = ""
    active: int = 0           # cuántas alertas tiene disparadas o pendientes


@dataclass(frozen=True)
class Target:
    """Un target de scrape, como lo reporta el backend."""

    job: str
    instance: str
    health: str               # "up" | "down" | "unknown"
    last_error: str = ""
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def up(self) -> bool:
        return self.health == "up"


@runtime_checkable
class MetricsPort(Protocol):
    """Lectura sobre el TSDB del cliente. No hay un solo método que escriba.

    Dos atributos opcionales que el core lee con `getattr` y default:

      query_syntax     qué acepta `query`. Vacío = PromQL. Un backend que no
                       habla PromQL (Cloud Eye) lo describe acá, y eso entra
                       al prompt del sistema: el modelo escribe lo que el
                       backend entiende, sin que el core sepa cuál es.
      supports_targets False si el backend no tiene targets de scrape. La
                       tool `targets_health` se oculta en vez de contestar
                       "0 de 0", que suena a "todo bien" y es "no aplica".
    """

    name: str

    async def instant(self, query: str, *, at: datetime | None = None) -> Instant:
        """Valor actual (o en `at`) de una expresión."""
        ...

    async def range(
        self,
        query: str,
        *,
        start: datetime,
        end: datetime,
        step_s: float | None = None,
    ) -> Range:
        """Serie temporal. `step_s=None` deja que el adapter elija uno que
        respete `max_points` — pedirle al modelo que calcule el step es pedirle
        que se equivoque."""
        ...

    async def label_values(self, label: str, *, matches: list[str] | None = None) -> list[str]:
        """Valores de un label. Es la tool con la que el agente descubre qué hay
        antes de escribir una query."""
        ...

    async def targets(self) -> list[Target]:
        """Targets de scrape con su salud."""
        ...

    async def alerts(self) -> list[Alert]:
        """Alertas firing o pending según el evaluador de reglas del backend."""
        ...

    async def rules(self) -> list[Rule]:
        """Las reglas de alerta configuradas, con su expresión y su estado."""
        ...

    async def check(self) -> None:
        """Ping. Levanta `MetricsError` si el backend no está usable.
        Lo usa `/readyz` y el comando `preflight`."""
        ...
