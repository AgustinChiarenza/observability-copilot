"""Corre los detectores habilitados, cada uno a su intervalo.

Es un `asyncio.Task` por detector dentro del proceso de la API, no un cron ni
un contenedor aparte: el producto es UN contenedor que se enchufa, y sumarle un
segundo proceso para algo que corre una vez por hora es duplicar el deploy para
nada. Si un día hace falta HA de verdad, es el momento de sacar esto a un
worker; hoy la duplicación sería el error.

La primera corrida se demora un rato corto y no arranca ya: en el arranque el
Prometheus del cliente puede estar reiniciándose junto con nosotros, y un
"no_data" en el primer minuto no es información.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .detectors import cost_spike

logger = logging.getLogger(__name__)

_FIRST_DELAY_S = 30.0


async def _loop(rt: Any, name: str, interval_s: float, run) -> None:
    await asyncio.sleep(_FIRST_DELAY_S)
    while True:
        try:
            await run(rt)
        except Exception as e:  # el detector ya loguea lo suyo; esto es el cinturón
            logger.warning("scheduler[%s]: %s: %s", name, type(e).__name__, e)
        await asyncio.sleep(interval_s)


def start(rt: Any) -> list[asyncio.Task]:
    tareas: list[asyncio.Task] = []
    cs = rt.config.detectors.cost_spike
    if cs.enabled and rt.cost is not None:
        tareas.append(asyncio.create_task(
            _loop(rt, cost_spike.NAME, cs.interval.total_seconds(), cost_spike.run_once),
            name=f"detector:{cost_spike.NAME}"))
        logger.info("scheduler: %s cada %s", cost_spike.NAME, cs.interval)
    return tareas


async def stop(tareas: list[asyncio.Task]) -> None:
    for t in tareas:
        t.cancel()
    for t in tareas:
        with contextlib.suppress(asyncio.CancelledError):
            await t
