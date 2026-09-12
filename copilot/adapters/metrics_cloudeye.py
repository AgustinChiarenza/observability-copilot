"""Cloud Eye (CES) de Huawei Cloud como MetricsPort.

Cloud Eye no habla PromQL, y este adapter no lo finge. `query` acepta un
selector chico que se parece a un vector de PromQL pero no tiene funciones:

    SYS.ECS/cpu_util{instance_id="abc-123"}       una métrica de un recurso
    SYS.ECS/cpu_util                              la misma, de todos los recursos
    max(SYS.RDS/rds001_cpu_util)                  con otra agregación (avg por defecto)

El core no sabe nada de esto: el adapter declara `query_syntax` y el prompt del
sistema lo incluye. Es la única forma honesta de meter un backend que no es
Prometheus sin inventar un DSL intermedio "universal", que fue descartado en
`ports/metrics.py` por buenas razones.

Lo que hay que saber de CES para leer este archivo:

  - las métricas son por recurso (dimensión). Sin dimensiones, se descubren
    con `list_metrics` y se piden todas en un batch (hasta 500 por llamada);
    el presupuesto `max_series` acota cuántas.
  - `period` es 1 (crudo), 300, 1200, 3600, 14400 u 86400 segundos. Se elige
    el menor que respete `max_points`.
  - las alertas están en dos lugares: las reglas (`list_alarm_rules`, API v2)
    y los disparos (`list_alarm_histories`, `status=alarm`). No hay `for:`
    sino `count × period`.
  - no hay targets de scrape: `supports_targets = False` y `targets_health`
    no aparece en el catálogo.

Sólo lectura: los tres métodos que usa el SDK son `list`/`batch_list`/`show`.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..ports.metrics import (
    Alert,
    Budget,
    Instant,
    Metadata,
    MetricsError,
    Range,
    Rule,
    Sample,
    Series,
    Target,
)
from . import register

logger = logging.getLogger(__name__)

QUERY_SYNTAX = """\
- `NAMESPACE/metric_name{dim="valor", ...}` — namespace y métrica de Cloud Eye
  (ej: SYS.ECS/cpu_util, SYS.RDS/rds001_cpu_util, SYS.EVS/disk_device_read_bytes_rate).
- Sin `{...}` se consultan TODOS los recursos que tienen esa métrica (acotado
  por el presupuesto). Con `{instance_id="..."}` se acota a uno.
- Agregación opcional: avg(...) (default), max(...), min(...), sum(...).
- NO hay funciones PromQL (rate, sum by, topk, etc.). Para "cuál es el peor",
  pedí la métrica sin dimensiones y mirá el resumen por serie.
- Descubrí qué hay con label_values: label="__name__" lista `NAMESPACE/metric`;
  label="namespace" lista los namespaces; label="instance_id" (o cualquier
  dimensión) lista sus valores, acotando con matches=["SYS.ECS/cpu_util"].
"""

_PERIODS = (1, 300, 1200, 3600, 14400, 86400)
_AGG = {"avg": "average", "max": "max", "min": "min", "sum": "sum"}
_RE_QUERY = re.compile(
    r"^\s*(?:(avg|max|min|sum)\s*\(\s*)?"
    r"([A-Za-z][A-Za-z0-9_.]*)/([A-Za-z][A-Za-z0-9_.]*)"
    r"\s*(\{[^}]*\})?\s*\)?\s*$")
_RE_DIM = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"')
_BATCH = 500
_META_TTL_S = 120.0


@dataclass(frozen=True)
class Selector:
    namespace: str
    metric: str
    dims: dict[str, str]
    agg: str = "avg"

    @property
    def name(self) -> str:
        return f"{self.namespace}/{self.metric}"


def parse_query(raw: str) -> Selector:
    m = _RE_QUERY.match(raw or "")
    if not m:
        raise MetricsError(
            f"'{raw}' no es un selector válido de Cloud Eye. La forma es "
            f"NAMESPACE/metric_name{{dim=\"valor\"}}, por ejemplo "
            f"SYS.ECS/cpu_util{{instance_id=\"abc\"}}. No hay funciones PromQL.",
            retriable=False, query=raw)
    agg, ns, metric, dims_raw = m.groups()
    dims = dict(_RE_DIM.findall(dims_raw or ""))
    if dims_raw and dims_raw.strip("{} ") and not dims:
        raise MetricsError(
            f"'{dims_raw}' no se entiende: las dimensiones van como "
            f'dim="valor", entre comillas dobles.', retriable=False, query=raw)
    return Selector(ns, metric, dims, agg or "avg")


@register("metrics", "cloudeye")
class CloudEyeMetrics:
    query_syntax = QUERY_SYNTAX
    supports_targets = False

    def __init__(
        self,
        *,
        region: str = "",
        ak: str = "",
        sk: str = "",
        project_id: str = "",
        endpoint: str = "",
        budget: Budget | None = None,
        name: str = "cloudeye",
        client_v1: Any = None,
        client_v2: Any = None,
        **_ignored: Any,
    ):
        faltan = [k for k, v in (("region", region), ("ak", ak), ("sk", sk)) if not v]
        if faltan and client_v1 is None:
            raise ValueError(
                f"metrics[{name}]: faltan {', '.join(faltan)}. La credencial "
                f"necesita sólo ces:metricData:list, ces:metrics:list y "
                f"ces:alarms:list.")
        self.name = name
        self.budget = budget or Budget()
        self._v1 = client_v1
        self._v2 = client_v2
        if client_v1 is None:
            self._v1, self._v2 = self._build(region, ak, sk, project_id or None, endpoint)
        self._meta: tuple[float, list[dict]] | None = None

    @staticmethod
    def _build(region: str, ak: str, sk: str, project_id: str | None, endpoint: str):
        try:
            from huaweicloudsdkces.v1 import CesClient as V1
            from huaweicloudsdkces.v2 import CesClient as V2
            from huaweicloudsdkces.v2.region.ces_region import CesRegion
            from huaweicloudsdkcore.auth.credentials import BasicCredentials
        except ImportError as e:
            raise ValueError("El adapter cloudeye necesita el SDK de Huawei: "
                             "pip install 'observability-copilot[huawei]'") from e
        cred = BasicCredentials(ak, sk, project_id)

        def armar(cls):
            b = cls.new_builder().with_credentials(cred)
            if endpoint:
                return b.with_endpoint(endpoint).build()
            try:
                return b.with_region(CesRegion.value_of(region)).build()
            except Exception as e:
                raise ValueError(f"metrics: región CES '{region}' desconocida para el "
                                 f"SDK. Pasá `endpoint` si es nueva o privada.") from e
        return armar(V1), armar(V2)

    # --- transporte ---------------------------------------------------------

    async def _call(self, fn, req, *, what: str) -> Any:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, req), self.budget.timeout_s)
        except TimeoutError as e:
            raise MetricsError(f"{self.name}: timeout a los {self.budget.timeout_s:.0f}s en {what}.",
                               retriable=True) from e
        except Exception as e:
            texto = str(e)[:300]
            # Las excepciones del SDK traen `status_code`. 429 y 5xx se
            # reintentan; el resto es "tu pedido está mal" y no.
            status = getattr(e, "status_code", 0) or 0
            raise MetricsError(f"{self.name}: {what} falló — {type(e).__name__}: {texto}",
                               retriable=(status == 429 or status >= 500)) from e

    # --- metadata -----------------------------------------------------------

    async def _metrics_meta(self) -> list[dict]:
        """Todas las (namespace, metric, dims) que CES conoce en el proyecto.
        Es la lista que sirve para descubrir y para expandir un selector sin
        dimensiones. Se cachea un rato: cambia cuando se crea un recurso, no
        entre dos preguntas del mismo turno."""
        if self._meta and time.monotonic() - self._meta[0] < _META_TTL_S:
            return self._meta[1]
        from huaweicloudsdkces.v1 import ListMetricsRequest

        salida: list[dict] = []
        marker = None
        for _ in range(50):
            resp = await self._call(
                self._v1.list_metrics, ListMetricsRequest(limit=1000, start=marker),
                what="list_metrics")
            salida.extend({
                "namespace": m.namespace, "metric": m.metric_name,
                "dims": {d.name: d.value for d in (m.dimensions or [])},
                "unit": getattr(m, "unit", "") or "",
            } for m in resp.metrics or [])
            marker = getattr(getattr(resp, "meta_data", None), "marker", None)
            if not marker or not resp.metrics:
                break
        self._meta = (time.monotonic(), salida)
        return salida

    async def _expand(self, sel: Selector) -> list[dict[str, str]]:
        """Las dimensiones concretas que hay que pedir para un selector."""
        if sel.dims:
            return [sel.dims]
        todas = [m["dims"] for m in await self._metrics_meta()
                 if m["namespace"] == sel.namespace and m["metric"] == sel.metric]
        if not todas:
            raise MetricsError(
                f"No hay ningún recurso con la métrica {sel.name}. Mirá qué existe "
                f"con label_values(label='__name__', contains='{sel.metric[:12]}').",
                retriable=False, query=sel.name)
        return todas

    # --- lectura ------------------------------------------------------------

    def pick_period(self, span: timedelta) -> int:
        minimo = span.total_seconds() / max(1, self.budget.max_points)
        for p in _PERIODS:
            if p >= minimo:
                return p
        return _PERIODS[-1]

    async def _fetch(self, sel: Selector, start: datetime, end: datetime, period: int,
                     ) -> tuple[list[Series], bool]:
        from huaweicloudsdkces.v1 import (
            BatchListMetricDataRequest,
            BatchListMetricDataRequestBody,
            MetricInfo,
            MetricsDimension,
        )

        dims_todas = await self._expand(sel)
        truncado = len(dims_todas) > self.budget.max_series
        dims_todas = dims_todas[: self.budget.max_series]
        campo = _AGG[sel.agg]
        series: list[Series] = []
        for i in range(0, len(dims_todas), _BATCH):
            lote = dims_todas[i:i + _BATCH]
            body = BatchListMetricDataRequestBody(
                metrics=[MetricInfo(
                    namespace=sel.namespace, metric_name=sel.metric,
                    dimensions=[MetricsDimension(name=k, value=v) for k, v in d.items()],
                ) for d in lote],
                period=str(period), filter=campo,
                _from=int(start.timestamp() * 1000), to=int(end.timestamp() * 1000),
            )
            resp = await self._call(
                self._v1.batch_list_metric_data, BatchListMetricDataRequest(body=body),
                what="batch_list_metric_data")
            for m in resp.metrics or []:
                labels = {"__name__": f"{m.namespace}/{m.metric_name}"}
                labels.update({d.name: d.value for d in (m.dimensions or [])})
                if getattr(m, "unit", None):
                    labels["unit"] = m.unit
                puntos = [
                    Sample(datetime.fromtimestamp(dp.timestamp / 1000, tz=UTC),
                           float(getattr(dp, campo, None) or 0.0))
                    for dp in sorted(m.datapoints or [], key=lambda x: x.timestamp)
                    if getattr(dp, campo, None) is not None
                ]
                series.append(Series(labels=labels, samples=puntos))
        return series, truncado

    async def instant(self, query: str, *, at: datetime | None = None) -> Instant:
        sel = parse_query(query)
        momento = at or datetime.now(UTC)
        # Los últimos 5 minutos a período 1: el último punto es "ahora".
        series, truncado = await self._fetch(sel, momento - timedelta(minutes=5), momento, 1)
        ultimos = [Series(labels=s.labels, samples=[s.samples[-1]]) for s in series if s.samples]
        return Instant(query=query, at=momento, series=ultimos, truncated=truncado)

    async def range(
        self, query: str, *, start: datetime, end: datetime, step_s: float | None = None,
    ) -> Range:
        sel = parse_query(query)
        if end <= start:
            raise MetricsError("range: `end` tiene que ser posterior a `start`.",
                               retriable=False, query=query)
        span = end - start
        if span > self.budget.max_range:
            raise MetricsError(
                f"range: se pidieron {span.days} días y el tope es "
                f"{self.budget.max_range.days}.", retriable=False, query=query)
        period = self.pick_period(span)
        if step_s and step_s > period:
            period = next((p for p in _PERIODS if p >= step_s), _PERIODS[-1])
        series, truncado = await self._fetch(sel, start, end, period)
        return Range(query=query, start=start, end=end, step_s=float(period),
                     series=series, truncated=truncado)

    async def label_values(self, label: str, *, matches: list[str] | None = None) -> list[str]:
        etiqueta = (label or "").strip()
        if not etiqueta:
            raise MetricsError("label_values: falta el nombre del label.", retriable=False)
        meta = await self._metrics_meta()
        if matches:
            quiero = {parse_query(m).name for m in matches}
            meta = [m for m in meta if f"{m['namespace']}/{m['metric']}" in quiero]
        if etiqueta == "__name__":
            valores = {f"{m['namespace']}/{m['metric']}" for m in meta}
        elif etiqueta == "namespace":
            valores = {m["namespace"] for m in meta}
        else:
            valores = {m["dims"][etiqueta] for m in meta if etiqueta in m["dims"]}
        return sorted(valores)[: self.budget.max_series]

    async def metadata(self, *, contains: str = "", limit: int = 100) -> list[Metadata]:
        # CES no tiene `help` ni tipo: todo es un gauge ya muestreado (los
        # contadores vienen como tasa por período). La unidad sí la manda, y es
        # lo que más sirve: `%` vs `byte/s` vs `count` cambia la respuesta.
        aguja = (contains or "").strip().lower()
        vistos: dict[str, str] = {}
        for m in await self._metrics_meta():
            nombre = f"{m['namespace']}/{m['metric']}"
            if aguja and aguja not in nombre.lower():
                continue
            vistos.setdefault(nombre, m["unit"])
        return [Metadata(name=n, type="gauge", unit=u)
                for n, u in sorted(vistos.items())[: max(1, limit)]]

    async def targets(self) -> list[Target]:
        raise MetricsError(
            "Cloud Eye no tiene targets de scrape. Para ver qué recursos reportan, "
            "usá label_values con label='instance_id'.", retriable=False)

    # --- alertas ------------------------------------------------------------

    async def alerts(self) -> list[Alert]:
        from huaweicloudsdkces.v2 import ListAlarmHistoriesRequest

        ahora = datetime.now(UTC)
        req = ListAlarmHistoriesRequest(
            status="alarm", limit=100,
            _from=int((ahora - timedelta(days=1)).timestamp() * 1000),
            to=int(ahora.timestamp() * 1000))
        resp = await self._call(self._v2.list_alarm_histories, req, what="list_alarm_histories")
        salida: list[Alert] = []
        for h in (resp.alarm_histories or [])[: self.budget.max_series]:
            metric = getattr(h, "metric", None)
            cond = getattr(h, "condition", None)
            labels = {"alarm_id": h.alarm_id or "", "alertname": h.name or "",
                      "severity": _level(getattr(h, "level", None))}
            if metric is not None:
                labels["metric"] = f"{metric.namespace}/{metric.metric_name}"
                labels.update({d.name: d.value for d in (metric.dimensions or [])})
            extra = getattr(h, "additional_info", None)
            if extra is not None and getattr(extra, "resource_name", None):
                labels["resource_name"] = extra.resource_name
            salida.append(Alert(
                name=h.name or "", state="firing", labels=labels,
                annotations={"summary": _condition_text(labels.get("metric", ""), cond)},
                active_at=_dt(getattr(h, "first_alarm_time", None) or getattr(h, "begin_time", None)),
                value="",
            ))
        return salida

    async def rules(self) -> list[Rule]:
        from huaweicloudsdkces.v2 import ListAlarmRulesRequest

        resp = await self._call(self._v2.list_alarm_rules, ListAlarmRulesRequest(limit=100),
                                what="list_alarm_rules")
        salida: list[Rule] = []
        for a in (resp.alarms or [])[: self.budget.max_series]:
            politicas = a.policies or []
            p = politicas[0] if politicas else None
            ns = a.namespace or (getattr(p, "namespace", "") if p else "")
            metric = f"{ns}/{p.metric_name}" if p and getattr(p, "metric_name", None) else ns
            recursos = sum(len(getattr(r, "dimensions", None) or []) or 1
                           for r in (a.resources or []))
            salida.append(Rule(
                name=a.name or a.alarm_id or "",
                expression=_condition_text(metric, p, agg=True),
                group=ns,
                state="inactive" if a.enabled else "disabled",
                duration_s=float((getattr(p, "period", 0) or 0) * (getattr(p, "count", 1) or 1))
                if p else 0.0,
                labels={"alarm_id": a.alarm_id or "",
                        "severity": _level(getattr(p, "level", None)) if p else "",
                        "resources": str(recursos)},
                annotations={"description": a.description or ""} if a.description else {},
                health="ok" if a.enabled else "err",
                last_error="" if a.enabled else "la regla está deshabilitada en Cloud Eye",
            ))
        return salida

    async def check(self) -> None:
        from huaweicloudsdkces.v1 import ListMetricsRequest

        await self._call(self._v1.list_metrics, ListMetricsRequest(limit=1), what="list_metrics")


def _level(level: Any) -> str:
    return {1: "critical", 2: "major", 3: "minor", 4: "info"}.get(level, "")


def _condition_text(metric: str, cond: Any, *, agg: bool = False) -> str:
    if cond is None:
        return metric
    filtro = getattr(cond, "filter", "") or "average"
    op = getattr(cond, "comparison_operator", "") or ">"
    valor = getattr(cond, "value", "")
    unidad = getattr(cond, "unit", "") or ""
    periodo = getattr(cond, "period", 0) or 0
    veces = getattr(cond, "count", 1) or 1
    base = f"{filtro}({metric}) {op} {valor}{unidad}"
    return f"{base} durante {veces}×{periodo}s" if agg else base


def _dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
