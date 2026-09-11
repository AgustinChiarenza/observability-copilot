"""Los puertos: lo único que el core conoce.

Un puerto es un `Protocol` — una forma, no una clase base. El core pide
`MetricsPort` y le da igual si atrás hay un Prometheus, un Thanos o un doble de
test; los adapters no heredan de nada ni importan nada del core, así que se
pueden escribir sin mirar este paquete.

La regla que hace que esto valga la pena: **ningún módulo de `copilot/` fuera de
`adapters/` puede importar un SDK de proveedor**. Si mañana el costo llega por
otro lado, se escribe un adapter y se cambia una línea de YAML. El día que un
`import huaweicloudsdkbssintl` aparezca en el core, el producto dejó de ser
enchufable y nadie se va a dar cuenta hasta el segundo cliente.
"""
from .alerts import Signal, SignalStatus
from .cost import CostPoint, CostPort, CostSlice
from .metrics import (
    Alert,
    Instant,
    MetricsError,
    MetricsPort,
    Range,
    Rule,
    Sample,
    Series,
    Target,
)
from .model import ModelPort, ModelReply
from .notify import Delivery, Message, NotifyPort, Severity

__all__ = [
    "Alert",
    "CostPoint",
    "CostPort",
    "CostSlice",
    "Delivery",
    "Instant",
    "Message",
    "MetricsError",
    "MetricsPort",
    "ModelPort",
    "ModelReply",
    "NotifyPort",
    "Range",
    "Rule",
    "Sample",
    "Series",
    "Severity",
    "Signal",
    "SignalStatus",
    "Target",
]
