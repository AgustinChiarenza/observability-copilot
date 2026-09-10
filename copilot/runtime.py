"""El armado: config → adapters → puertos listos para usar.

Todo el conocimiento de "qué adapter va en qué puerto" vive acá y en ningún otro
lado. El resto del código pide `rt.metrics` y no sabe —ni puede saber— qué hay
atrás.

Dos decisiones que se ven raras y son a propósito:

- **El CostPort recibe el MetricsPort.** El adapter de costos por defecto lee el
  gasto de la misma TSDB donde ya están las métricas, así que necesita al otro
  puerto. Es la dependencia que hace posible que la instalación más común no
  pida ninguna credencial de facturación.
- **Si no hay canales configurados se agrega el de log.** Un YAML de notify con
  un error de tipeo produciría un sistema que parece sano, no falla y no avisa
  nada. Con el canal de log ese silencio queda escrito en algún lado.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import adapters
from .agent.registry import Context
from .config import Config
from .ports.cost import CostPort
from .ports.metrics import MetricsPort
from .ports.model import ModelPort
from .ports.notify import NotifyPort

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    config: Config
    metrics: MetricsPort | None = None
    cost: CostPort | None = None
    model: ModelPort | None = None
    notify: list[NotifyPort] = field(default_factory=list)

    def context(self) -> Context:
        """Lo que ven las tools."""
        return Context(metrics=self.metrics, cost=self.cost)

    def describe(self) -> dict[str, Any]:
        """Qué quedó enchufado. Lo devuelve `/v1/status` y lo imprime preflight."""
        return {
            "metrics": getattr(self.metrics, "name", None),
            "cost": getattr(self.cost, "name", None),
            "model": getattr(self.model, "name", None),
            "model_name": getattr(self.model, "default_model", None),
            "notify": [c.name for c in self.notify],
        }

    async def check(self) -> dict[str, str]:
        """Toca cada puerto y devuelve `{puerto: "ok" | motivo}`.

        No corta al primer fallo: quien corre esto quiere la lista completa de
        lo que está mal, no descubrirlos de a uno con un reinicio en el medio.
        Lo usan `/readyz` y `copilot preflight` —el comando que se corre en lo
        del cliente antes de irse.
        """
        salida: dict[str, str] = {}
        for nombre, puerto in (("metrics", self.metrics), ("cost", self.cost),
                               ("model", self.model)):
            if puerto is None:
                salida[nombre] = "not configured"
                continue
            try:
                await puerto.check()
                salida[nombre] = "ok"
            except Exception as e:
                salida[nombre] = f"{type(e).__name__}: {e}"
        for canal in self.notify:
            try:
                await canal.check()
                salida[f"notify:{canal.name}"] = "ok"
            except Exception as e:
                salida[f"notify:{canal.name}"] = f"{type(e).__name__}: {e}"
        return salida


def build(config: Config) -> Runtime:
    """Instancia los adapters que pide la config. Falla fuerte y con nombre."""
    rt = Runtime(config=config)

    if config.metrics:
        rt.metrics = adapters.build(
            "metrics", config.metrics.adapter, config.metrics.options,
            budget=config.budget, name=config.metrics.name)

    if config.cost:
        # El adapter de costos por PromQL necesita el de métricas. Los que van
        # contra una API de facturación lo ignoran (`**_ignored`), así que se
        # pasa siempre en vez de preguntar por el nombre del adapter — que sería
        # el core volviendo a conocer nombres propios.
        rt.cost = adapters.build(
            "cost", config.cost.adapter, config.cost.options,
            metrics=rt.metrics, name=config.cost.name)

    if config.model:
        rt.model = adapters.build(
            "model", config.model.adapter, config.model.options, name=config.model.name)

    for canal in config.notify:
        rt.notify.append(
            adapters.build("notify", canal.adapter, canal.options, name=canal.name))

    if not rt.notify:
        logger.warning(
            "notify: no hay canales configurados; las notificaciones van al log.")
        rt.notify.append(adapters.build("notify", "log", {}, name="log"))

    logger.info("runtime armado: %s", rt.describe())
    return rt
