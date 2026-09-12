"""El detector de pico: la regla está en código, no en un prompt, y se testea."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from copilot.adapters.notify_log import LogNotifier
from copilot.detectors import cost_spike
from copilot.detectors.cost_spike import SpikeConfig, evaluate
from copilot.dispatch import Dispatcher
from copilot.ports.cost import CostPoint, CostSlice


class FakeCost:
    name = "fake"

    def __init__(self, amounts: list[float], lag_days: int = 0, services=None):
        self.lag_days = lag_days
        self.amounts = amounts
        self.services = services or []
        self.asked: list[tuple[date, date]] = []

    async def daily_series(self, *, start, end):
        self.asked.append((start, end))
        dias = (end - start).days + 1
        montos = self.amounts[-dias:]
        return [CostPoint(day=end - timedelta(days=len(montos) - 1 - i), amount=a)
                for i, a in enumerate(montos)]

    async def by_service(self, *, start, end, limit=10):
        return self.services[:limit]

    async def by_resource(self, *, start, end, limit=20, service=None):
        return []

    async def check(self):
        return


CFG = SpikeConfig(enabled=True, window_days=7, threshold=1.5, min_days=5)


async def test_un_dia_al_doble_de_la_mediana_es_pico():
    v = await evaluate(FakeCost([100, 100, 100, 100, 100, 100, 200]), CFG)
    assert v.outcome == "spike"
    assert v.finding.ratio == 2.0 and v.finding.median == 100


async def test_la_mediana_no_incluye_el_ultimo_dia():
    """Si lo incluyera, un pico se compararía parcialmente consigo mismo y se
    achicaría solo."""
    v = await evaluate(FakeCost([100, 100, 100, 100, 100, 100, 1000]), CFG)
    assert v.finding.median == 100


async def test_un_pico_previo_no_tapa_el_siguiente():
    """Con promedio, el 500 del medio subiría el baseline y el 180 pasaría
    desapercibido. Con mediana, no."""
    v = await evaluate(FakeCost([100, 100, 500, 100, 100, 100, 180]), CFG)
    assert v.outcome == "spike"


async def test_por_debajo_del_umbral_esta_limpio():
    v = await evaluate(FakeCost([100, 100, 100, 100, 100, 100, 140]), CFG)
    assert v.outcome == "clear"
    assert "1.40×" in v.detail


async def test_min_amount_evita_gritar_por_monedas():
    cfg = SpikeConfig(window_days=7, threshold=1.5, min_amount=50)
    v = await evaluate(FakeCost([2, 2, 2, 2, 2, 2, 6]), cfg)
    assert v.outcome == "clear"


async def test_sin_baseline_no_opina():
    v = await evaluate(FakeCost([100, 300]), CFG)
    assert v.outcome == "no_data"
    assert "baseline" in v.detail


async def test_respeta_el_lag_del_puerto():
    """El día que todavía se está llenando no se mira: siempre parece raro."""
    port = FakeCost([100] * 7, lag_days=1)
    await evaluate(port, CFG)
    _, hasta = port.asked[-1]
    assert hasta == datetime.now(UTC).date() - timedelta(days=1)


async def test_el_aviso_trae_el_desglose_y_un_fingerprint_por_dia():
    port = FakeCost([100, 100, 100, 100, 100, 100, 400],
                    services=[CostSlice("ecs", 300), CostSlice("obs", 50)])
    v = await evaluate(port, CFG)
    m = cost_spike.to_message(v.finding)
    assert m.fingerprint == f"cost_spike:{v.finding.day}"
    assert "4.0×" in m.title
    assert "ecs: 300.00" in m.body
    assert m.severity == "critical"    # 4× ya no es un warning


async def test_run_once_despacha_y_la_segunda_corrida_se_deduplica(config):
    from copilot.runtime import Runtime

    canal = LogNotifier(name="ops")
    rt = Runtime(config=config, cost=FakeCost([100] * 6 + [250]),
                 dispatcher=Dispatcher([canal]))
    r1 = await cost_spike.run_once(rt)
    r2 = await cost_spike.run_once(rt)
    assert r1["outcome"] == "spike" and r1["notify"]["decision"] == "sent"
    assert r2["notify"]["decision"] == "deduped"
    assert len(canal.sent) == 1


async def test_run_once_sin_costos_lo_dice(config):
    from copilot.runtime import Runtime

    r = await cost_spike.run_once(Runtime(config=config))
    assert r["outcome"] == "no_data"


@pytest.mark.parametrize("raw,campo", [
    ({"threshold": 1.0}, "threshold"),
    ({"window_days": 3, "min_days": 5}, "window_days"),
])
def test_la_config_rechaza_umbrales_sin_sentido(raw, campo):
    from copilot.config import Config

    cfg = Config.from_dict({
        "server": {"api_token": "t"},
        "metrics": {"adapter": "prometheus", "url": "http://p"},
        "model": {"adapter": "openai_compat", "base_url": "http://m/v1", "model": "x"},
        "detectors": {"cost_spike": raw},
    })
    assert any(campo in p for p in cfg.problems())


# --- Lo que el modelo ve tiene que coincidir con lo que dispara ------------------


async def test_cost_daily_usa_la_misma_mediana_que_el_detector():
    """Si el detector dice 10× y la tool le dice al modelo 1.3×, el aviso y la
    explicación se contradicen delante del cliente."""
    from copilot.agent.registry import Context, execute

    cost = FakeCost([100, 100, 100, 100, 100, 100, 1000])
    r = (await execute("cost_daily", {"days": 7}, Context(cost=cost)))["result"]
    v = await evaluate(cost, CFG)
    assert r["median_day"] == 100 and r["last_vs_median"] == 10.0
    assert v.finding is not None and round(v.finding.ratio, 2) == r["last_vs_median"]


async def test_cost_daily_acota_la_ventana():
    """Un `days: 3650` que se le ocurra al modelo no puede bajar diez años de
    facturación."""
    from copilot.agent.registry import Context, execute
    from copilot.agent.tools_cost import MAX_DAYS

    cost = FakeCost([100] * 10)
    await execute("cost_daily", {"days": 3650}, Context(cost=cost))
    desde, hasta = cost.asked[-1]
    assert (hasta - desde).days + 1 == MAX_DAYS


async def test_el_pico_no_se_repite_cada_hora_mientras_siga_siendo_el_ultimo_dia():
    from copilot.detectors.cost_spike import to_message

    v = await evaluate(FakeCost([100, 100, 100, 100, 100, 100, 200]), CFG)
    m = to_message(v.finding)
    assert m.repeat_after is not None and m.repeat_after >= timedelta(days=1)
