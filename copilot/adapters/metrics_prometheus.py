"""Adapter de la HTTP API v1 de Prometheus.

Un adapter, seis backends: Prometheus, Thanos Query, Mimir, VictoriaMetrics,
Grafana Cloud y AMP hablan todos este dialecto. Por eso el chequeo de salud es
`query=1` y no `/api/v1/status/buildinfo`: buildinfo es de Prometheus y los
compatibles lo devuelven distinto o no lo devuelven, así que usarlo para el
readiness haría fallar el arranque contra backends que funcionan perfecto.

**El presupuesto se aplica acá.** Es lo único que separa "el agente consulta las
métricas del cliente" de "el agente le puede tirar abajo el Prometheus de
producción". Tres cosas:

  - el `step` de una query de rango lo calcula el adapter, no el que la pide. Un
    modelo pidiendo 30 días con step de 15s son 172.800 puntos por serie;
    prometerle que elija bien es delegar el incidente.
  - las series que pasan `max_series` se descartan y el resultado sale marcado
    `truncated`. Marcado, no en silencio: quien lee tiene que saber que está
    viendo una parte.
  - un selector sin ningún matcher se rechaza antes de salir a la red.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

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

# Pasos "redondos". Un step de 137s produce timestamps que no se alinean con
# nada y hace ilegible cualquier comparación entre dos queries.
_STEPS = (15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400)

# `{}` o `{__name__=~".+"}`: piden el universo entero. Contra un Prometheus
# grande eso es un incidente, no una consulta.
_RE_EMPTY_SELECTOR = re.compile(r"\{\s*\}")


def _f(raw: Any) -> float:
    """Los valores vienen como string. NaN e Inf son legales en PromQL."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return math.nan


def _ts(raw: Any) -> datetime:
    return datetime.fromtimestamp(float(raw), tz=UTC)


def _rfc3339(raw: Any) -> datetime | None:
    """`activeAt` viene como RFC 3339 con nanosegundos ("...T10:00:00.123456789Z").
    `fromisoformat` acepta hasta seis decimales, así que se recorta. El cero de
    Go ("0001-01-01T00:00:00Z") significa "nunca" y vuelve como None."""
    if not raw or str(raw).startswith("0001-01-01"):
        return None
    texto = str(raw).replace("Z", "+00:00")
    m = re.match(r"^(.*\.\d{1,6})\d*([+-].*)$", texto)
    if m:
        texto = m.group(1) + m.group(2)
    try:
        return datetime.fromisoformat(texto)
    except ValueError:
        return None


@register("metrics", "prometheus")
class PrometheusMetrics:
    """Lectura sobre cualquier backend compatible con Prometheus."""

    def __init__(
        self,
        *,
        url: str = "",
        auth: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        verify_tls: bool = True,
        budget: Budget | None = None,
        name: str = "prometheus",
        transport: httpx.AsyncBaseTransport | None = None,
        **_ignored: Any,
    ):
        if not url:
            raise ValueError(
                "metrics.url: falta. Es la URL base del Prometheus del cliente, "
                "por ejemplo http://prometheus.monitoring:9090")
        self.name = name
        self.url = url.rstrip("/")
        self.budget = budget or Budget()
        self._verify = verify_tls
        self._headers = dict(headers or {})
        self._auth: httpx.Auth | None = None
        # Costura para los tests: permite correr el parseo real contra
        # respuestas falsas sin levantar un Prometheus. No se puede setear
        # desde el YAML (las opciones de config son escalares), así que no es
        # una puerta que alguien pueda abrir por accidente en producción.
        self._transport = transport

        # Los cuatro modos que aparecen en la vida real. El passthrough de
        # headers cubre lo que no entra en ninguno (AMP con un proxy SigV4
        # adelante, un gateway corporativo con su propio header).
        modo = (auth or {}).get("type", "none")
        if modo == "bearer":
            token = (auth or {}).get("token", "")
            if not token:
                raise ValueError("metrics.auth.token: falta con auth.type=bearer.")
            self._headers["Authorization"] = f"Bearer {token}"
        elif modo == "basic":
            user, pwd = (auth or {}).get("username", ""), (auth or {}).get("password", "")
            if not user:
                raise ValueError("metrics.auth.username: falta con auth.type=basic.")
            self._auth = httpx.BasicAuth(user, pwd)
        elif modo not in ("none", None, ""):
            raise ValueError(
                f"metrics.auth.type: '{modo}' no se conoce. Usá none, bearer o basic "
                f"(para lo demás, pasá headers).")

    # --- transporte ---------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any]) -> dict:
        try:
            async with httpx.AsyncClient(
                timeout=self.budget.timeout_s, verify=self._verify,
                headers=self._headers, auth=self._auth, transport=self._transport,
            ) as c:
                r = await c.get(f"{self.url}{path}", params=params)
        except httpx.TimeoutException as e:
            raise MetricsError(
                f"{self.name}: timeout a los {self.budget.timeout_s:.0f}s. La query "
                f"pide demasiado, o el backend está saturado.",
                retriable=True, query=str(params.get("query", "")),
            ) from e
        except httpx.HTTPError as e:
            raise MetricsError(f"{self.name}: no se pudo alcanzar {self.url} ({e}).",
                               retriable=True) from e

        if r.status_code >= 500:
            raise MetricsError(f"{self.name}: el backend devolvió {r.status_code}.",
                               retriable=True)
        # 400 y 422 son "tu query está mal": reintentar no la arregla, y el
        # cuerpo trae el error de parseo de PromQL, que es lo único útil acá.
        if r.status_code in (400, 422):
            raise MetricsError(
                f"{self.name}: query rechazada — {_error_body(r)}",
                retriable=False, query=str(params.get("query", "")))
        if r.status_code >= 400:
            raise MetricsError(f"{self.name}: {r.status_code} — {_error_body(r)}")

        data = r.json()
        if data.get("status") != "success":
            raise MetricsError(
                f"{self.name}: {data.get('error') or 'respuesta sin éxito'}",
                retriable=False, query=str(params.get("query", "")))
        return data.get("data") or {}

    # --- lectura ------------------------------------------------------------

    def _guard(self, query: str) -> str:
        q = (query or "").strip()
        if not q:
            raise MetricsError("query vacía.", retriable=False)
        if _RE_EMPTY_SELECTOR.search(q):
            raise MetricsError(
                "Un selector vacío `{}` pide todas las series del backend. "
                "Agregá al menos un matcher, por ejemplo {job=\"node\"}.",
                retriable=False, query=q)
        return q

    def _series(self, result: list[dict], *, matrix: bool) -> tuple[list[Series], bool]:
        recorte = result[: self.budget.max_series]
        salida: list[Series] = []
        for item in recorte:
            labels = dict(item.get("metric") or {})
            if matrix:
                puntos = [Sample(_ts(t), _f(v)) for t, v in (item.get("values") or [])]
            else:
                par = item.get("value") or []
                puntos = [Sample(_ts(par[0]), _f(par[1]))] if len(par) == 2 else []
            salida.append(Series(labels=labels, samples=puntos))
        return salida, len(result) > len(recorte)

    async def instant(self, query: str, *, at: datetime | None = None) -> Instant:
        q = self._guard(query)
        momento = at or datetime.now(UTC)
        data = await self._get("/api/v1/query",
                               {"query": q, "time": momento.timestamp()})
        tipo = data.get("resultType")
        crudo = data.get("result") or []
        # Un `scalar` viene como [ts, "valor"] pelado, no como lista de series.
        if tipo == "scalar" and len(crudo) == 2:
            crudo = [{"metric": {}, "value": crudo}]
        series, truncado = self._series(list(crudo), matrix=False)
        if truncado:
            logger.info("%s: instant truncada a %d series", self.name, self.budget.max_series)
        return Instant(query=q, at=momento, series=series, truncated=truncado)

    def pick_step(self, span: timedelta) -> float:
        """Step que respeta `max_points`, redondeado hacia arriba a uno usual."""
        minimo = span.total_seconds() / max(1, self.budget.max_points)
        for s in _STEPS:
            if s >= minimo:
                return float(s)
        return float(_STEPS[-1])

    async def range(
        self, query: str, *, start: datetime, end: datetime, step_s: float | None = None,
    ) -> Range:
        q = self._guard(query)
        if end <= start:
            raise MetricsError("range: `end` tiene que ser posterior a `start`.",
                               retriable=False, query=q)
        span = end - start
        if span > self.budget.max_range:
            raise MetricsError(
                f"range: se pidieron {span.days} días y el tope es "
                f"{self.budget.max_range.days}. Subí budget.max_range si de "
                f"verdad hace falta mirar tan atrás.",
                retriable=False, query=q)

        step = float(step_s) if step_s else self.pick_step(span)
        # Aun con step explícito el techo manda: es el único que protege al
        # backend del cliente, y quien lo pasó puede no saber cuánto abarca.
        if span.total_seconds() / step > self.budget.max_points:
            step = self.pick_step(span)

        data = await self._get("/api/v1/query_range", {
            "query": q, "start": start.timestamp(), "end": end.timestamp(), "step": step,
        })
        series, truncado = self._series(list(data.get("result") or []), matrix=True)
        return Range(query=q, start=start, end=end, step_s=step,
                     series=series, truncated=truncado)

    async def label_values(self, label: str, *, matches: list[str] | None = None) -> list[str]:
        etiqueta = (label or "").strip()
        if not etiqueta:
            raise MetricsError("label_values: falta el nombre del label.", retriable=False)
        params: dict[str, Any] = {}
        if matches:
            params["match[]"] = matches
        data = await self._get(f"/api/v1/label/{etiqueta}/values", params)
        valores = data if isinstance(data, list) else []
        return [str(v) for v in valores[: self.budget.max_series]]

    async def metadata(self, *, contains: str = "", limit: int = 100) -> list[Metadata]:
        # /api/v1/metadata filtra por nombre exacto (`metric=`), no por
        # substring; se pide todo y se filtra acá. Son unos KB por métrica
        # expuesta, y la lista es la misma en todo el turno.
        data = await self._get("/api/v1/metadata", {})
        aguja = (contains or "").strip().lower()
        salida: list[Metadata] = []
        for nombre in sorted((data or {}).keys()):
            if aguja and aguja not in nombre.lower():
                continue
            # Un mismo nombre puede venir con metadata distinta de dos targets;
            # se toma la primera, es lo que hace la UI de Prometheus también.
            fichas = data[nombre] or [{}]
            m = fichas[0]
            salida.append(Metadata(
                name=nombre, type=str(m.get("type") or "unknown"),
                help=str(m.get("help") or "")[:300], unit=str(m.get("unit") or "")))
            if len(salida) >= max(1, limit):
                break
        return salida

    async def targets(self) -> list[Target]:
        data = await self._get("/api/v1/targets", {"state": "active"})
        activos = (data or {}).get("activeTargets") or []
        salida: list[Target] = []
        for t in activos[: self.budget.max_series]:
            labels = dict(t.get("labels") or {})
            salida.append(Target(
                job=labels.get("job", ""),
                instance=labels.get("instance", ""),
                health=str(t.get("health") or "unknown").lower(),
                last_error=str(t.get("lastError") or ""),
                labels=labels,
            ))
        return salida

    async def alerts(self) -> list[Alert]:
        data = await self._get("/api/v1/alerts", {})
        crudas = (data or {}).get("alerts") or []
        salida: list[Alert] = []
        for a in crudas[: self.budget.max_series]:
            labels = dict(a.get("labels") or {})
            salida.append(Alert(
                name=labels.get("alertname", ""),
                state=str(a.get("state") or "firing").lower(),
                labels=labels,
                annotations=dict(a.get("annotations") or {}),
                active_at=_rfc3339(a.get("activeAt")),
                value=str(a.get("value") or ""),
            ))
        return salida

    async def rules(self) -> list[Rule]:
        # `type=alert` deja afuera las recording rules. Un backend viejo que no
        # conozca el parámetro lo ignora y se filtra igual acá abajo.
        data = await self._get("/api/v1/rules", {"type": "alert"})
        salida: list[Rule] = []
        for grupo in (data or {}).get("groups") or []:
            for r in grupo.get("rules") or []:
                if r.get("type") != "alerting":
                    continue
                salida.append(Rule(
                    name=str(r.get("name") or ""),
                    expression=str(r.get("query") or ""),
                    group=str(grupo.get("name") or ""),
                    state=str(r.get("state") or "inactive").lower(),
                    duration_s=float(r.get("duration") or 0),
                    labels=dict(r.get("labels") or {}),
                    annotations=dict(r.get("annotations") or {}),
                    health=str(r.get("health") or "unknown"),
                    last_error=str(r.get("lastError") or ""),
                    active=len(r.get("alerts") or []),
                ))
                if len(salida) >= self.budget.max_series:
                    return salida
        return salida

    async def check(self) -> None:
        # `query=1` lo contesta cualquier backend compatible. buildinfo no.
        await self._get("/api/v1/query", {"query": "1"})


def _error_body(r: httpx.Response) -> str:
    try:
        j = r.json()
        return str(j.get("error") or j.get("errorType") or r.text)[:300]
    except ValueError:
        return r.text[:300]
