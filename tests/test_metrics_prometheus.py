"""El adapter de métricas: parseo real y, sobre todo, el presupuesto.

El presupuesto es lo único que separa "el agente consulta las métricas del
cliente" de "el agente le puede tirar abajo el Prometheus de producción". Si
algo de este archivo se vuelve incómodo de mantener, la respuesta no es aflojar
los topes.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from copilot.adapters.metrics_prometheus import PrometheusMetrics
from copilot.ports.metrics import Budget, MetricsError
from tests.conftest import matrix, vector


async def test_lee_un_vector(metrics_port, prom):
    prom.series['up'] = [vector({"__name__": "up", "job": "node"}, 1.0)]
    r = await metrics_port.instant("up")
    assert len(r) == 1
    assert r.series[0].labels["job"] == "node"
    assert r.series[0].last.value == 1.0


async def test_lee_una_matriz(metrics_port, prom):
    prom.series['rate(x[5m])'] = [
        matrix({"job": "a"}, [(1700000000, 1.0), (1700000060, 2.0)])]
    fin = datetime.now(UTC)
    r = await metrics_port.range("rate(x[5m])", start=fin - timedelta(hours=1), end=fin)
    assert [s.value for s in r.series[0].samples] == [1.0, 2.0]


async def test_un_escalar_no_rompe(metrics_port):
    """`query=1` devuelve [ts, "1"] pelado, con otra forma que un vector. Es la
    query del healthcheck, así que si esto rompe no arranca nada."""
    r = await metrics_port.instant("1")
    assert r.series[0].last.value == 1.0


async def test_un_nan_sobrevive_como_nan(metrics_port, prom):
    """PromQL devuelve NaN legítimamente. Convertirlo a 0 lo haría pasar por un
    dato real y el resumen mentiría."""
    prom.series["q"] = [vector({}, 0.0)]
    prom.series["q"][0]["value"] = [1700000000, "NaN"]
    r = await metrics_port.instant("q")
    assert math.isnan(r.series[0].last.value)


# --- Presupuesto ------------------------------------------------------------


async def test_las_series_de_mas_se_descartan_y_queda_marcado(prom):
    """Truncar en silencio es devolver una parte como si fuera el todo: nadie
    lo nota hasta que la conclusión ya está tomada."""
    port = PrometheusMetrics(url="http://p", transport=prom.transport(),
                             budget=Budget(max_series=3))
    prom.series["muchas"] = [vector({"i": str(i)}, float(i)) for i in range(50)]
    r = await port.instant("muchas")
    assert len(r.series) == 3
    assert r.truncated is True


async def test_el_step_lo_elige_el_adapter_para_no_pasarse_de_puntos(prom):
    """Un modelo pidiendo 30 días con step de 15s son 172.800 puntos por serie.
    Confiar en que elija bien es delegar el incidente."""
    port = PrometheusMetrics(url="http://p", transport=prom.transport(),
                             budget=Budget(max_points=1000))
    fin = datetime.now(UTC)
    await port.range("x", start=fin - timedelta(days=7), end=fin)
    _, params = prom.calls[-1]
    step = float(params["step"])
    assert timedelta(days=7).total_seconds() / step <= 1000


async def test_un_step_explicito_no_puede_saltear_el_techo(prom):
    """Quien lo pasó puede no saber cuánto abarca la ventana."""
    port = PrometheusMetrics(url="http://p", transport=prom.transport(),
                             budget=Budget(max_points=100))
    fin = datetime.now(UTC)
    r = await port.range("x", start=fin - timedelta(days=7), end=fin, step_s=15)
    assert r.step_s > 15


async def test_un_rango_mas_largo_que_el_tope_se_rechaza(prom):
    port = PrometheusMetrics(url="http://p", transport=prom.transport(),
                             budget=Budget(max_range=timedelta(days=7)))
    fin = datetime.now(UTC)
    with pytest.raises(MetricsError) as e:
        await port.range("x", start=fin - timedelta(days=30), end=fin)
    assert "budget.max_range" in str(e.value)


async def test_un_selector_vacio_no_llega_a_la_red(metrics_port, prom):
    """`{}` pide todas las series del backend. Contra un Prometheus grande eso
    es un incidente, no una consulta."""
    with pytest.raises(MetricsError):
        await metrics_port.instant("{}")
    assert prom.calls == []


# --- Errores ----------------------------------------------------------------


async def test_una_query_mal_escrita_no_es_reintentable(metrics_port, prom):
    """Distinguir 400 de 500 es lo que evita que un error de sintaxis se
    reintente en loop contra el backend del cliente."""
    prom.fail_with = 400
    with pytest.raises(MetricsError) as e:
        await metrics_port.instant("sum(")
    assert e.value.retriable is False


async def test_un_backend_caido_si_es_reintentable(metrics_port, prom):
    prom.fail_with = 503
    with pytest.raises(MetricsError) as e:
        await metrics_port.instant("up")
    assert e.value.retriable is True


# --- Targets y labels -------------------------------------------------------


async def test_lee_los_targets_con_su_salud(metrics_port, prom):
    prom.targets = [
        {"labels": {"job": "node", "instance": "a:9100"}, "health": "up"},
        {"labels": {"job": "node", "instance": "b:9100"}, "health": "down",
         "lastError": "connection refused"},
    ]
    ts = await metrics_port.targets()
    assert [t.up for t in ts] == [True, False]
    assert "refused" in ts[1].last_error


async def test_lista_valores_de_un_label(metrics_port, prom):
    prom.labels["job"] = ["node", "prometheus", "copilot"]
    assert await metrics_port.label_values("job") == ["node", "prometheus", "copilot"]


async def test_el_check_usa_una_query_que_todos_los_compatibles_contestan(metrics_port, prom):
    """No buildinfo: es de Prometheus y los compatibles lo devuelven distinto o
    no lo devuelven, así que el readiness fallaría contra backends sanos."""
    await metrics_port.check()
    path, params = prom.calls[-1]
    assert path == "/api/v1/query"
    assert params["query"] == "1"


def test_una_url_faltante_lo_dice_al_construir():
    with pytest.raises(ValueError, match=r"metrics\.url"):
        PrometheusMetrics()


def test_un_tipo_de_auth_desconocido_lo_dice():
    with pytest.raises(ValueError, match=r"auth\.type"):
        PrometheusMetrics(url="http://p", auth={"type": "kerberos"})
