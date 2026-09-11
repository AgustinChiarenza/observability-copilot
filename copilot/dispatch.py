"""El despachante: decide si un mensaje sale, y por qué canales.

Los canales son tontos a propósito (ver `ports/notify.py`): reciben y entregan.
Todo lo que es *política* vive acá, una sola vez para todos los canales:

  dedup       el mismo `fingerprint` no se repite antes de `repeat_interval`.
              Un detector que corre cada hora y ve el mismo pico avisa una vez,
              no veinticuatro.
  quiet hours una ventana horaria en la que lo que no es crítico espera. Lo
              crítico pasa igual: la regla protege el sueño, no esconde el
              incendio.
  tope diario por canal. Cuando se alcanza, sale UN aviso más diciendo que se
              alcanzó, y después nada hasta el día siguiente. Es la línea que
              separa "un bug en el detector" de "200 SMS una madrugada", que es
              el error que te desinstala.
  recorte     al `max_chars` que declara cada canal. Un SMS no puede con 3.000
              caracteres y no tiene por qué saberlo el que redacta.

Un `RESOLVED` no se deduplica contra el disparo que cierra: es la otra mitad de
la misma conversación y tiene que llegar. Y borra la marca del disparo, así el
próximo se avisa aunque venga antes del `repeat_interval`.

El estado es en memoria. Un reinicio olvida los dedups —lo peor que pasa es un
aviso repetido— y los topes del día, que es el caso a mirar cuando llegue la
persistencia (F7).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import telemetry
from .ports.notify import Delivery, Message, NotifyPort, Severity
from .store import State

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuietHours:
    """`start`/`end` como "22:00"/"07:00" en la zona horaria `tz`. Puede cruzar
    la medianoche, que es el caso normal."""

    start: time
    end: time
    tz: str = "UTC"

    def covers(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(self.tz)).time()
        if self.start <= self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end


@dataclass(frozen=True)
class Policy:
    repeat_interval: timedelta = timedelta(hours=4)
    quiet_hours: QuietHours | None = None
    daily_cap: int = 50


@dataclass
class Outcome:
    """Qué pasó con un mensaje. `decision` es una de:
    sent | deduped | quiet | capped | no_channels."""

    fingerprint: str
    title: str
    severity: str
    decision: str
    deliveries: list[Delivery] = field(default_factory=list)
    at: str = ""

    @property
    def sent(self) -> bool:
        return self.decision == "sent"

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at, "fingerprint": self.fingerprint, "title": self.title,
            "severity": self.severity, "decision": self.decision,
            "deliveries": [{"channel": d.channel, "ok": d.ok, "detail": d.detail}
                           for d in self.deliveries],
        }


def _fit(m: Message, max_chars: int) -> Message:
    if len(m.body) <= max_chars:
        return m
    return Message(
        title=m.title, body=m.body[: max(0, max_chars - 1)] + "…",
        severity=m.severity, fingerprint=m.fingerprint, url=m.url, labels=m.labels,
    )


class Dispatcher:
    def __init__(self, channels: list[NotifyPort], policy: Policy | None = None,
                 state: State | None = None):
        self.channels = list(channels)
        self.policy = policy or Policy()
        self._last_sent: dict[str, datetime] = {}
        self._sent_today: dict[tuple[str, date], int] = {}
        self._cap_notified: set[tuple[str, date]] = set()
        self.history: deque[Outcome] = deque(maxlen=500)
        # El estado es la política que sobrevive al reinicio: sin él, el
        # proceso nuevo no sabe a quién le avisó el viejo ni cuántas veces hoy.
        self._state = state
        if state is not None:
            self._restore(state.load())

    # --- estado en disco ----------------------------------------------------

    def _restore(self, d: dict[str, Any]) -> None:
        try:
            self._last_sent = {str(k): datetime.fromisoformat(v)
                               for k, v in (d.get("last_sent") or {}).items()}
            self._sent_today = {(str(c), date.fromisoformat(f)): int(n)
                                for c, f, n in (d.get("sent_today") or [])}
            self._cap_notified = {(str(c), date.fromisoformat(f))
                                  for c, f in (d.get("cap_notified") or [])}
        except (ValueError, TypeError) as e:
            logger.warning("dispatch: estado ilegible, se arranca vacío (%s)", e)
            self._last_sent, self._sent_today, self._cap_notified = {}, {}, set()

    def _snapshot(self, now: datetime) -> None:
        if self._state is None:
            return
        hoy = now.date()
        # Lo de ayer no cuenta para el tope de hoy, y un fingerprint que ya
        # pasó el repeat_interval no deduplica nada: se podan al guardar.
        self._sent_today = {k: v for k, v in self._sent_today.items() if k[1] == hoy}
        self._cap_notified = {k for k in self._cap_notified if k[1] == hoy}
        self._last_sent = {k: v for k, v in self._last_sent.items()
                           if now - v < self.policy.repeat_interval}
        self._state.save({
            "last_sent": {k: v.isoformat() for k, v in self._last_sent.items()},
            "sent_today": [[c, f.isoformat(), n] for (c, f), n in self._sent_today.items()],
            "cap_notified": [[c, f.isoformat()] for c, f in self._cap_notified],
        })

    # --- política -----------------------------------------------------------

    def decide(self, m: Message, *, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        if not self.channels:
            return "no_channels"
        if m.severity is Severity.RESOLVED:
            return "sent"
        if m.fingerprint:
            previo = self._last_sent.get(m.fingerprint)
            if previo and now - previo < self.policy.repeat_interval:
                return "deduped"
        qh = self.policy.quiet_hours
        if qh and m.severity is not Severity.CRITICAL and qh.covers(now):
            return "quiet"
        return "sent"

    async def send(self, m: Message, *, force: bool = False,
                   now: datetime | None = None) -> Outcome:
        """`force` saltea dedup, quiet hours y tope. Es para el mensaje de prueba
        de la instalación —"¿llega?"— y no para nada que corra solo."""
        now = now or datetime.now(UTC)
        decision = "sent" if force and self.channels else self.decide(m, now=now)
        out = Outcome(m.fingerprint, m.title, str(m.severity), decision, at=now.isoformat())

        if decision == "sent":
            for canal in self.channels:
                d = await self._deliver(canal, m, now, force=force)
                if d is not None:
                    out.deliveries.append(d)
            if not out.deliveries:
                out.decision = "capped"
            elif m.fingerprint:
                if m.severity is Severity.RESOLVED:
                    self._last_sent.pop(m.fingerprint, None)
                else:
                    self._last_sent[m.fingerprint] = now
            self._snapshot(now)   # también si quedó "capped": cambió el contador

        telemetry.NOTIFY_OUTCOMES.labels(decision=out.decision).inc()
        if out.decision != "sent":
            logger.info("notify: '%s' no salió (%s)", m.title, out.decision)
        self.history.append(out)
        return out

    async def _deliver(self, canal: NotifyPort, m: Message, now: datetime,
                       *, force: bool) -> Delivery | None:
        llave = (canal.name, now.date())
        enviados = self._sent_today.get(llave, 0)
        if not force and enviados >= self.policy.daily_cap:
            if llave not in self._cap_notified:
                self._cap_notified.add(llave)
                aviso = Message(
                    title="Copilot: tope diario de notificaciones alcanzado",
                    body=(f"Este canal ya recibió {enviados} mensajes hoy "
                          f"(dispatch.daily_cap). Los siguientes se descartan "
                          f"hasta mañana. Si esto pasa seguido, hay un detector "
                          f"gritando o el tope es bajo."),
                    severity=Severity.WARNING, fingerprint=f"cap:{canal.name}",
                )
                await self._try(canal, aviso)
            logger.warning("notify[%s]: tope diario (%d) alcanzado; se descarta '%s'",
                           canal.name, self.policy.daily_cap, m.title)
            telemetry.NOTIFY_DELIVERIES.labels(channel=canal.name, ok="capped").inc()
            return None
        d = await self._try(canal, _fit(m, canal.max_chars))
        self._sent_today[llave] = enviados + 1
        telemetry.NOTIFY_DELIVERIES.labels(channel=canal.name, ok=str(d.ok).lower()).inc()
        return d

    @staticmethod
    async def _try(canal: NotifyPort, m: Message) -> Delivery:
        # Un canal que explota no puede frenar a los demás: queda como entrega
        # fallida y se sigue.
        try:
            return await canal.send(m)
        except Exception as e:
            logger.warning("notify[%s]: %s: %s", canal.name, type(e).__name__, e)
            return Delivery(canal.name, False, f"{type(e).__name__}: {e}")

    def summary(self) -> dict[str, Any]:
        hoy = datetime.now(UTC).date()
        return {
            "channels": [c.name for c in self.channels],
            "policy": {
                "repeat_interval_s": self.policy.repeat_interval.total_seconds(),
                "quiet_hours": (
                    {"start": self.policy.quiet_hours.start.isoformat("minutes"),
                     "end": self.policy.quiet_hours.end.isoformat("minutes"),
                     "tz": self.policy.quiet_hours.tz}
                    if self.policy.quiet_hours else None),
                "daily_cap": self.policy.daily_cap,
            },
            "sent_today": {c.name: self._sent_today.get((c.name, hoy), 0)
                           for c in self.channels},
            "durable": self._state is not None,
            "recent": [o.as_dict() for o in list(self.history)[-20:]],
        }
