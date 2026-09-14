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

Lo que está en vuelo cuenta. Mientras el firing de un fingerprint se enriquece
—un triage puede tardar más que lo que dura una alerta corta—, el despachante
todavía no sabe que salió. Sin esto pasaban dos cosas, y las dos se vieron
contra un Alertmanager real: el mismo firing reenviado por `group_interval`
se procesaba dos veces (dos triages, dos avisos), y la resuelta que llegaba
antes de que terminara el triage se descartaba como `unpaired` — el operador
recibía el disparo y nunca el cierre. Ahora un firing repetido en vuelo se
saltea (`inflight`) y una resuelta espera a que su firing termine, y recién
ahí se decide.

`AlertLog` es la memoria de lo que llegó y qué se hizo. Anillo en memoria y,
con `storage.path`, un JSONL al que se anexa cada alerta cuando termina de
procesarse —no al llegar: se guarda con su decisión y su enriquecimiento, que
es lo que vale releer—. Es lo que lee `GET /v1/alerts`, la tool
`alerts_received` y, en F6, el bot cuando alguien pregunta "¿qué pasó anoche?".
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
from ..store import Jsonl
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
            "annotations": s.annotations, "source": s.source,
            "received_at": self.received_at, "finished_at": self.finished_at,
            "decision": self.decision, "enrichment": self.enrichment,
            "deliveries": self.deliveries,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Record:
        """El inverso de `as_dict`, para recargar del disco."""
        ends = d.get("ends_at")
        return cls(
            signal=Signal(
                fingerprint=str(d["fingerprint"]), name=str(d["name"]),
                status=SignalStatus(d.get("status", "firing")),
                starts_at=datetime.fromisoformat(d["starts_at"]),
                labels=dict(d.get("labels") or {}),
                annotations=dict(d.get("annotations") or {}),
                ends_at=datetime.fromisoformat(ends) if ends else None,
                source=str(d.get("source") or "alertmanager"),
            ),
            received_at=str(d["received_at"]),
            decision=str(d.get("decision") or "pending"),
            enrichment=d.get("enrichment"),
            deliveries=list(d.get("deliveries") or []),
            finished_at=str(d.get("finished_at") or ""),
        )


class AlertLog:
    def __init__(self, maxlen: int = 1_000, store: Jsonl | None = None):
        self._items: deque[Record] = deque(maxlen=maxlen)
        self._store = store
        if store is not None:
            for fila in store.tail(maxlen):
                try:
                    self._items.append(Record.from_dict(fila))
                except (KeyError, ValueError, TypeError):
                    continue   # una línea de otra versión

    @property
    def durable(self) -> bool:
        return self._store is not None

    def add(self, r: Record) -> None:
        self._items.append(r)

    def persist(self, r: Record) -> None:
        """Se llama cuando la alerta terminó (decidida o entregada): eso es lo
        que vale guardar. Sin store es un no-op."""
        if self._store is not None:
            self._store.append(r.as_dict())

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
        #: El firing en vuelo por fingerprint, para que su repetición se saltee
        #: y su resuelta lo espere.
        self._inflight: dict[str, asyncio.Task] = {}

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
            en_vuelo = self._inflight.get(s.fingerprint)
            if en_vuelo is not None and en_vuelo.done():
                en_vuelo = None
            if en_vuelo is not None and s.status is SignalStatus.FIRING:
                decision = "inflight"
            elif en_vuelo is not None:
                # La resuelta de algo que todavía se está enriqueciendo: se
                # decide cuando el firing termine, no ahora que el
                # despachante aún no lo vio salir.
                decision = "sent"
            else:
                # Decidir ahora, enriquecer después: una deduplicada no gasta tokens.
                decision = self._rt.dispatcher.decide(to_message(s, None))
            if decision == "sent" and len(self._pending) >= self._cfg.queue_max:
                decision = "overloaded"
            if decision != "sent":
                rec.decision, rec.finished_at = decision, ahora
                self._log.persist(rec)
                telemetry.ALERTS_PROCESSED.labels(decision=decision).inc()
                salteadas.append({"fingerprint": s.fingerprint, "decision": decision})
                continue
            corrida = self._after(en_vuelo, rec) if en_vuelo is not None else self._process(rec)
            t = asyncio.create_task(corrida, name=f"alert:{s.fingerprint}")
            self._pending.add(t)
            t.add_done_callback(self._pending.discard)
            if s.status is SignalStatus.FIRING:
                self._inflight[s.fingerprint] = t
                t.add_done_callback(lambda done, fp=s.fingerprint: (
                    self._inflight.pop(fp, None) if self._inflight.get(fp) is done else None))
            encoladas.append(s.fingerprint)
        return {"received": len(señales), "queued": encoladas, "skipped": salteadas,
                "rejected": rechazos}

    async def _after(self, previo: asyncio.Task, rec: Record) -> None:
        """Una resuelta que llegó con su firing en vuelo: espera, y recién ahí
        pasa por la política (que ahora sí sabe si el disparo salió)."""
        await asyncio.wait({previo})
        decision = self._rt.dispatcher.decide(to_message(rec.signal, None))
        if decision != "sent":
            rec.decision = decision
            rec.finished_at = datetime.now(UTC).isoformat()
            self._log.persist(rec)
            telemetry.ALERTS_PROCESSED.labels(decision=rec.decision).inc()
            return
        await self._process(rec)

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
            self._log.persist(rec)
            telemetry.ALERTS_PROCESSED.labels(decision=out.decision).inc()
            logger.info("alerts: %s %s → %s (%s)", s.status, s.name, out.decision,
                        ", ".join(f"{d.channel}:{'ok' if d.ok else 'FALLA'}"
                                  for d in out.deliveries) or "-")

    async def drain(self) -> None:
        """Espera lo que quedó en vuelo. Lo usan los tests y el apagado."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
