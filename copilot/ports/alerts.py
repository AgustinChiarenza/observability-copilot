"""Signal: una alerta normalizada, venga de donde venga.

Deliberadamente calcada del webhook de Alertmanager, y eso es una decisión, no
una comodidad. El cliente ya tiene reglas y un Alertmanager funcionando: con
`for:`, inhibición, silences y dedup en HA. Escribir un evaluador de umbrales
propio sería competir contra un componente en el que ya confían, con menos
funcionalidad y sin su historial. Este producto es un addon — recibe lo que ese
Alertmanager ya decidió y le agrega lo que él no puede hacer: contexto,
correlación con costo y una explicación.

El corolario práctico: **el `fingerprint` lo pone Alertmanager, no nosotros.**
Ya viene calculado sobre el labelset y es estable entre disparos. Inventar uno
propio da un dedup que no coincide con el que el cliente ya ve en su UI, y
entonces "yo silencié eso" deja de ser cierto.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SignalStatus(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class Signal:
    """Una alerta lista para enriquecer."""

    fingerprint: str
    name: str
    status: SignalStatus
    starts_at: datetime
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    ends_at: datetime | None = None
    #: La expresión que la disparó, si el emisor la manda. Vale oro para el
    #: enriquecimiento: es la query que hay que correr en rango alrededor del
    #: disparo, sin tener que adivinarla.
    expression: str = ""
    source: str = "alertmanager"

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "warning")

    @property
    def summary(self) -> str:
        return self.annotations.get("summary") or self.name
