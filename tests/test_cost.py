"""El costo leído de la TSDB, que es el camino por defecto.

Si el cliente ya exporta su gasto a Prometheus, el dato está en casa: responde
en milisegundos, no pide credenciales nuevas y no tiene rate limit. Ir a la API
de facturación en ese escenario es pedir más permisos para llegar más tarde.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from copilot.adapters.cost_promql import PromqlCost
from copilot.agent.registry import Context, execute
from copilot.ports.cost import CostError
from tests.conftest import matrix, vector

DAILY = "sum(increase(cloud_cost_usd_total[1d]))"
BY_SVC = "sum by (service) (increase(cloud_cost_usd_total[1d]))"


@pytest.fixture
def cost(metrics_port):
    return PromqlCost(metrics=metrics_port, daily=DAILY, by_service=BY_SVC,
                      currency="USD")


def hoy() -> date:
    """La fecha de hoy en UTC, igual que la app.

    Con `hoy()` (fecha local) estos tests se ponen intermitentes: a las
    22:00 en Buenos Aires ya es el día siguiente en UTC, y la ventana que arma
    la tool deja de coincidir con la que arma el test.
    """
    return datetime.now(UTC).date()


def _ts(d: date) -> float:
    return datetime.combine(d, time.min, tzinfo=UTC).timestamp()


def _dias(montos: list[float], *, hasta: date | None = None) -> list[tuple[float, float]]:
    """Un punto por día terminando en `hasta`, en el orden de `montos`."""
    fin = hasta or hoy()
    n = len(montos)
    return [(_ts(fin - timedelta(days=n - 1 - i)), m) for i, m in enumerate(montos)]


async def test_la_serie_diaria_da_un_punto_por_dia(cost, prom):
    prom.series[DAILY] = [matrix({}, _dias([100.0, 101.0, 102.0, 103.0, 104.0]))]
    puntos = await cost.daily_series(start=hoy() - timedelta(days=4),
                                     end=hoy())
    assert len(puntos) == 5
    assert puntos[0].amount == 100.0
    assert puntos[-1].amount == 104.0


async def test_el_step_diario_se_fija_a_mano(cost, prom):
    """Un step de 6h sobre un `increase(...[1d])` devuelve cuatro puntos por día
    que se pisan, y la mediana del baseline sale calculada sobre ventanas
    solapadas: baseline inflado y picos que no existen."""
    prom.series[DAILY] = [matrix({}, _dias([10.0, 11.0, 12.0]))]
    await cost.daily_series(start=hoy() - timedelta(days=2), end=hoy())
    _, params = prom.calls[-1]
    assert float(params["step"]) == 86400.0


async def test_varias_series_se_suman_por_dia(cost, prom):
    """Si el cliente escribió una expresión que devuelve más de una serie,
    sumarlas es lo correcto y además lo que él esperaba."""
    dias = _dias([10.0, 10.0])
    prom.series[DAILY] = [matrix({"a": "1"}, dias), matrix({"a": "2"}, dias)]
    puntos = await cost.daily_series(start=hoy() - timedelta(days=1),
                                     end=hoy())
    assert puntos[0].amount == 20.0


async def test_el_desglose_ordena_de_mayor_a_menor(cost, prom):
    ventana = BY_SVC.replace("[1d]", "[7d]")
    prom.series[ventana] = [
        vector({"service": "ecs"}, 300.0),
        vector({"service": "obs"}, 900.0),
        vector({"service": "rds"}, 50.0),
    ]
    filas = await cost.by_service(start=hoy() - timedelta(days=6), end=hoy())
    assert [f.key for f in filas] == ["obs", "ecs", "rds"]


async def test_sin_expresion_de_desglose_lo_dice_con_el_campo(metrics_port):
    port = PromqlCost(metrics=metrics_port, daily=DAILY)
    with pytest.raises(CostError, match=r"cost\.by_service"):
        await port.by_service(start=hoy(), end=hoy())


def test_sin_expresion_diaria_no_se_puede_construir(metrics_port):
    """Es la única obligatoria: sin ella no hay detección de picos, que es la
    razón por la que este puerto existe."""
    with pytest.raises(ValueError, match=r"cost\.daily"):
        PromqlCost(metrics=metrics_port)


# --- La tool ----------------------------------------------------------------


async def test_la_tool_entrega_la_mediana_y_el_desvio_calculados(metrics_port, prom, cost):
    """El número que dispara una alarma se calcula en código y se testea. Si se
    lo dejás al modelo, a veces promedia en vez de sacar mediana — y ahí un pico
    previo se come el baseline."""
    # Seis días parejos y un salto el último: la mediana se queda en 100 y el
    # cociente da 3,5. Con promedio daría 2,7 y el pico se vería más chico.
    prom.series[DAILY] = [matrix({}, _dias([100.0] * 6 + [350.0]))]
    ctx = Context(metrics=metrics_port, cost=cost)
    r = await execute("cost_daily", {"days": 7}, ctx)
    res = r["result"]
    assert res["median_day"] == 100.0
    assert res["last_vs_median"] == 3.5
    assert res["last_day"]["amount"] == 350.0


async def test_la_ventana_se_recorta_por_el_atraso_del_backend(metrics_port, prom):
    """El día que todavía se está llenando siempre parece una caída del 60%. Es
    la falsa alarma más común de FinOps y sale gratis no cometerla."""
    port = PromqlCost(metrics=metrics_port, daily=DAILY, lag_days=1)
    ayer = hoy() - timedelta(days=1)
    prom.series[DAILY] = [matrix({}, _dias([10.0, 11.0, 12.0], hasta=ayer))]
    ctx = Context(metrics=metrics_port, cost=port)
    r = await execute("cost_daily", {"days": 3}, ctx)
    assert r["result"]["to"] == (hoy() - timedelta(days=1)).isoformat()
    assert r["result"]["lag_days"] == 1
