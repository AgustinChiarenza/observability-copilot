"""Del webhook de Alertmanager (versión 4) a `Signal`.

Se parsea a mano en vez de con un modelo Pydantic estricto por una razón: el
payload lo arma un componente que no controlamos y que evoluciona. Un campo
nuevo o un timestamp con un formato levemente distinto no tienen que rechazar
un lote de veinte alertas reales. Lo que no se entiende se ignora; lo que
falta y es esencial (el fingerprint, el nombre) descarta esa alerta sola, con
un aviso, y el resto sigue.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from ..ports.alerts import Signal, SignalStatus

logger = logging.getLogger(__name__)

_RE_FRAC = re.compile(r"^(.*\.\d{1,6})\d*([+-].*|Z)$")


def _when(raw: Any) -> datetime | None:
    """RFC 3339 con nanosegundos, y el cero de Go como "nunca"."""
    if not raw or str(raw).startswith("0001-01-01"):
        return None
    texto = str(raw)
    m = _RE_FRAC.match(texto)
    if m:
        texto = m.group(1) + m.group(2)
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse(payload: dict[str, Any]) -> tuple[list[Signal], list[str]]:
    """Devuelve las señales que se pudieron leer y los motivos de las que no."""
    crudas = payload.get("alerts")
    if not isinstance(crudas, list):
        return [], ["el payload no trae `alerts` como lista; ¿es el webhook de Alertmanager?"]

    comunes = dict(payload.get("commonLabels") or {})
    externo = str(payload.get("externalURL") or "")
    señales: list[Signal] = []
    rechazos: list[str] = []
    for i, a in enumerate(crudas):
        if not isinstance(a, dict):
            rechazos.append(f"alerts[{i}]: no es un objeto")
            continue
        labels = {**comunes, **dict(a.get("labels") or {})}
        nombre = labels.get("alertname", "")
        fp = str(a.get("fingerprint") or "")
        if not nombre or not fp:
            rechazos.append(f"alerts[{i}]: sin alertname o sin fingerprint")
            continue
        estado = SignalStatus.RESOLVED if a.get("status") == "resolved" else SignalStatus.FIRING
        señales.append(Signal(
            fingerprint=fp,
            name=nombre,
            status=estado,
            starts_at=_when(a.get("startsAt")) or datetime.now(UTC),
            ends_at=_when(a.get("endsAt")),
            labels=labels,
            annotations=dict(a.get("annotations") or {}),
            # Alertmanager no manda la expresión. Este campo queda para
            # emisores que sí (o para el enriquecimiento, que la busca).
            expression="",
            source=externo or "alertmanager",
        ))
    if rechazos:
        logger.warning("alertmanager: %d alerta(s) descartadas: %s", len(rechazos), rechazos[:3])
    return señales, rechazos
