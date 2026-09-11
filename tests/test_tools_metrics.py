"""Las tools de métricas que no tienen archivo propio todavía."""
from __future__ import annotations

import pytest

from copilot.agent.registry import Context, execute

pytestmark = pytest.mark.anyio


async def test_metric_metadata_dice_tipo_unidad_y_help(metrics_port, prom):
    """Sin esto el modelo le hace rate() a un gauge, o suma un counter sin
    rate() y contesta un número enorme que no significa nada."""
    prom.metadata = {
        "node_cpu_seconds_total": [{"type": "counter", "help": "Segundos de CPU.", "unit": ""}],
        "node_load1": [{"type": "gauge", "help": "Carga 1m", "unit": ""},
                       {"type": "gauge", "help": "otra versión", "unit": ""}],
        "http_request_duration_seconds": [{"type": "histogram", "help": "", "unit": "seconds"}],
    }
    ctx = Context(metrics=metrics_port)
    r = (await execute("metric_metadata", {"contains": "node"}, ctx))["result"]
    assert r["count"] == 2 and not r["truncated"]
    assert r["metrics"][0] == {"name": "node_cpu_seconds_total", "type": "counter",
                               "help": "Segundos de CPU."}
    assert r["metrics"][1]["help"] == "Carga 1m"   # la primera ficha, como la UI

    r = (await execute("metric_metadata", {"limit": 1}, ctx))["result"]
    assert r["count"] == 1 and r["truncated"]
    assert r["metrics"][0]["unit"] == "seconds"
