"""El scheduler arranca sólo lo que está habilitado Y tiene con qué correr."""
from __future__ import annotations

from copilot import scheduler
from copilot.config import Config
from copilot.runtime import Runtime
from tests.test_cost_spike import FakeCost


def _cfg(enabled: bool) -> Config:
    return Config.from_dict({
        "server": {"api_token": "t"},
        "metrics": {"adapter": "prometheus", "url": "http://p"},
        "model": {"adapter": "openai_compat", "base_url": "http://m/v1", "model": "x"},
        "detectors": {"cost_spike": {"enabled": enabled, "interval": "5m"}},
    })


async def test_deshabilitado_no_arranca_nada():
    assert scheduler.start(Runtime(config=_cfg(False), cost=FakeCost([1]))) == []


async def test_habilitado_sin_costos_tampoco():
    assert scheduler.start(Runtime(config=_cfg(True))) == []


async def test_habilitado_con_costos_arranca_y_se_puede_parar():
    tareas = scheduler.start(Runtime(config=_cfg(True), cost=FakeCost([1])))
    assert [t.get_name() for t in tareas] == ["detector:cost_spike"]
    await scheduler.stop(tareas)
    assert all(t.cancelled() for t in tareas)
