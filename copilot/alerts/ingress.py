"""Recibir, decidir, enriquecer, entregar — en ese orden, y no todo a la vez.

Alertmanager espera la respuesta del webhook en segundos (su timeout es 10s)
y un triage con el modelo puede tardar un minuto. Por eso `receive()` decide
por cada alerta si el aviso va a salir —dedup, quiet hours, tope— y contesta
202 con lo que encoló; el enriquecimiento corre atrás, bajo un semáforo, y
recién entonces entrega. Si se hiciera en línea, Alertmanager cortaría, lo
reintentaría y el mismo aviso llegaría tres veces.

El orden importa por plata: decidir es gratis, enriquecer cuesta tokens. Una
alerta que el despachante va a deduplicar no gasta un turno del agente para
después descartarse.

`AlertLog` es la memoria de lo que llegó y qué se hizo. Anillo en memoria,
como la auditoría: es lo que lee `GET /v1/alerts`, la tool `alerts_received`
y, en F6, el bot cuando alguien pregunta "¿qué pasó anoche?".
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .. import telemetry
from ..dispatch import Outcome
from ..ports.alerts import Signal, SignalStatus
from ..ports.notify import Message, Severity
from . import alertmanager, enrich

logger = logging.getLogger(__name__)

_SEVERITY = {"critical": Severity.CRITICAL, "warning": Severity.WARNING,
             "info": Severity.INFO, "none": Severity.INFO}


@dataclass
class Record:
    signal: Signal
    received_at: str
    decision: str = "pending"
    enrichment: dict[str, Any] | None = None
    deliveries: list[dict[str, Any]] = field(default_factory=list)
    finished_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        s = self.signal
        return {
            "fingerprint": s.fingerprint, "name": s.name, "status": str(s.status),
            "severity": s.severity, "labels": s.labels, "summary": s.summary,
            "starts_at": s.starts_at.isoformat(),
            "ends_at": s.ends_at.isoformat() if s.ends_at else None,
            "received_at": self.received_at, "finished_at": self.finished_at,
            "decision": self.decision, "enrichment": self.enrichment,
            "deliveries": self.deliveries,
        }


class AlertLog:
    def __init__(self, maxlen: int = 1_000):
        self._items: deque[Record] = deque(maxlen=maxlen)

    def add(self, r: Record) -> None:
        self._items.append(r)

    def recent(self, *, limit: int = 100, only_firing: bool = False,
               since: datetime | None = None) -> list[Record]:
        salida = []
        for r in reversed(self._items):
            if only_firing and r.signal.status is not SignalStatus.FIRING:
                continue
            if since and datetime.fromisoformat(r.received_at) < since:
                break
            salida.append(r)
            if len(salida) >= limit:
                break
        return salida

    def __len__(self) -> int:
        return len(self._items)


def to_message(s: Signal, e: enrich.Enrichment | None) -> Message:
    """Corto arriba (un SMS se queda con el título y una línea), el detalle
    abajo para los canales con lugar."""
    resuelta = s.status is SignalStatus.RESOLVED
    recurso = s.labels.get("instance") or s.labels.get("job") or s.labels.get("service") or ""
    titulo = f"[{'RESUELTA' if resuelta else s.severity.upper()}] {s.name}" + (
        f" — {recurso}" if recurso else "")
    lineas = [s.summary]
    etiquetas = ", ".join(f"{k}={v}" for k, v in sorted(s.labels.items())
                          if k not in ("alertname", "severity"))
    if etiquetas:
        lineas.append(etiquetas)
    if resuelta:
        lineas.append(f"Duró desde {s.starts_at.isoformat(timespec='minutes')}"
                      + (f" hasta {s.ends_at.isoformat(timespec='minutes')}" if s.ends_at else ""))
    elif e is not None:
        if e.expression:
            lineas.append(f"Regla: {e.expression}"
                          + (f" (durante {e.duration_s:.0f}s)" if e.duration_s else ""))
        for t in e.trend[:3]:
            resumen = {k: t[k] for k in ("first", "last", "min", "max", "value") if k in t}
            lineas.append(f"Tendencia {t['series']}: {resumen}")
        if e.active_alerts is not None:
            lineas.append(f"Alertas firing en total: {e.active_alerts}")
        if e.cost:
            lineas.append(f"Gasto: último día {e.cost['last_vs_median']}× la mediana")
        if e.triage:
            lineas.append("")
            lineas.append(e.triage)
    return Message(
        title=titulo, body="\n".join(x for x in lineas if x is not None),
        severity=Severity.RESOLVED if resuelta else _SEVERITY.get(s.severity, Severity.WARNING),
        fingerprint=s.fingerprint, url=s.annotations.get("runbook_url", ""),
        labels={"alertname": s.name, "source": s.source},
    )


class Ingress:
    def __init__(self, rt: Any, cfg: enrich.EnrichConfig, log: AlertLog):
        self._rt, self._cfg, self._log = rt, cfg, log
        self._sem = asyncio.Semaphore(max(1, cfg.max_concurrent))
        self._pending: set[asyncio.Task] = set()

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def receive(self, payload: dict[str, Any]) -> dict[str, Any]:
        señales, rechazos = alertmanager.parse(payload)
        ahora = datetime.now(UTC).isoformat()
        encoladas: list[str] = []
        salteadas: list[dict[str, str]] = []
        for s in señales:
            telemetry.ALERTS_RECEIVED.labels(status=str(s.status)).inc()
            rec = Record(signal=s, received_at=ahora)
            self._log.add(rec)
            # Decidir ahora, enriquecer después: una deduplicada no gasta tokens.
            decision = self._rt.dispatcher.decide(to_message(s, None))
            if decision == "sent" and len(self._pending) >= self._cfg.queue_max:
                decision = "overloaded"
            if decision != "sent":
                rec.decision, rec.finished_at = decision, ahora
                telemetry.ALERTS_PROCESSED.labels(decision=decision).inc()
                salteadas.append({"fingerprint": s.fingerprint, "decision": decision})
                continue
            t = asyncio.create_task(self._process(rec), name=f"alert:{s.fingerprint}")
            self._pending.add(t)
            t.add_done_callback(self._pending.discard)
            encoladas.append(s.fingerprint)
        return {"received": len(señales), "queued": encoladas, "skipped": salteadas,
                "rejected": rechazos}

    async def _process(self, rec: Record) -> None:
        s = rec.signal
        async with self._sem:
            e: enrich.Enrichment | None = None
            if s.status is SignalStatus.FIRING:
                try:
                    reglas = await enrich.rules_by_name(self._rt)
                    e = await enrich.enrich(self._rt, s, self._cfg, rules=reglas)
                    rec.enrichment = e.as_dict()
                except Exception as ex:  # el aviso sale igual, pelado
                    logger.warning("alerts: enriquecer %s falló: %s", s.name, ex)
                    rec.enrichment = {"errors": [f"{type(ex).__name__}: {ex}"]}
            out: Outcome = await self._rt.dispatcher.send(to_message(s, e))
            rec.decision = out.decision
            rec.deliveries = [{"channel": d.channel, "ok": d.ok, "detail": d.detail}
                              for d in out.deliveries]
            rec.finished_at = datetime.now(UTC).isoformat()
            telemetry.ALERTS_PROCESSED.labels(decision=out.decision).inc()
            logger.info("alerts: %s %s → %s (%s)", s.status, s.name, out.decision,
                        ", ".join(f"{d.channel}:{'ok' if d.ok else 'FALLA'}"
                                  for d in out.deliveries) or "-")

    async def drain(self) -> None:
        """Espera lo que quedó en vuelo. Lo usan los tests y el apagado."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
