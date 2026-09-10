"""Las tools: lo que devuelven, y cuánto contexto ocupan haciéndolo.

Un `query_range` de 200 series por 300 puntos son 60.000 números. Entran al
contexto, lo llenan, y el modelo termina razonando sobre menos información útil
que con un resumen. Por eso la mitad de estos tests son sobre la forma del
resultado y no sobre el valor.
"""
from __future__ import annotations

import pytest

from copilot.agent.registry import Context, execute
from tests.conftest import matrix, vector


@pytest.fixture
def ctx(metrics_port):
    return Context(metrics=metrics_port)


async def test_una_serie_corta_vuelve_punto_por_punto(ctx, prom):
    prom.series["x"] = [matrix({"job": "a"}, [(1700000000, 1.0), (1700000060, 2.0)])]
    r = await execute("promql_range", {"query": "x", "lookback": "1h"}, ctx)
    assert r["status"] == "ok"
    assert len(r["result"]["series"][0]["points"]) == 2


async def test_una_serie_larga_vuelve_resumida(ctx, prom):
    """Con 300 puntos, lo que el modelo necesita es mín/máx/promedio y la
    tendencia — no 300 números que le tapan el resto del turno."""
    puntos = [(1700000000 + i * 60, float(i)) for i in range(300)]
    prom.series["x"] = [matrix({"job": "a"}, puntos)]
    r = await execute("promql_range", {"query": "x", "lookback": "6h"}, ctx)
    fila = r["result"]["series"][0]
    assert fila["points"] == 300      # el conteo, no la lista
    assert fila["min"] == 0.0 and fila["max"] == 299.0
    assert fila["first"] == 0.0 and fila["last"] == 299.0


async def test_muchas_series_se_detallan_hasta_un_tope_y_lo_avisa(ctx, prom):
    prom.series["y"] = [vector({"i": str(i)}, float(i)) for i in range(80)]
    r = await execute("promql_instant", {"query": "y"}, ctx)
    res = r["result"]
    assert res["count"] == 80
    assert len(res["series"]) == 25
    assert "sum by" in res["note"]


async def test_un_resultado_truncado_sale_marcado_como_parcial(ctx, prom, metrics_port):
    """Una conclusión sacada de una muestra que el usuario cree completa es peor
    que no contestar."""
    metrics_port.budget = type(metrics_port.budget)(max_series=2)
    prom.series["z"] = [vector({"i": str(i)}, float(i)) for i in range(10)]
    r = await execute("promql_instant", {"query": "z"}, ctx)
    assert r["result"]["truncated"] is True
    assert "PARCIAL" in r["result"]["warning"]


async def test_un_nan_no_rompe_la_serializacion(ctx, prom):
    """NaN no es JSON válido: sin convertirlo, revienta el armado del turno."""
    prom.series["q"] = [{"metric": {}, "value": [1700000000, "NaN"]}]
    r = await execute("promql_instant", {"query": "q"}, ctx)
    import json
    json.dumps(r)   # no tiene que levantar
    assert r["result"]["series"][0]["value"] is None


async def test_las_ventanas_se_piden_en_criollo(ctx, prom):
    """Pedirle al modelo que calcule epochs es pedirle que se equivoque de zona
    horaria — y eso no da un error, da un gráfico plano que parece un dato."""
    prom.series["x"] = []
    r = await execute("promql_range", {"query": "x", "lookback": "7d"}, ctx)
    assert r["status"] == "ok"
    malo = await execute("promql_range", {"query": "x", "lookback": "un rato"}, ctx)
    assert malo["status"] == "error"
    assert "'30m', '6h', '7d'" in malo["result"]


async def test_descubrir_metricas_por_tema(ctx, prom):
    """Es la tool que evita que el modelo invente `cpu_usage_percent` en una
    instalación donde la métrica se llama `node_cpu_seconds_total`."""
    prom.labels["__name__"] = ["node_cpu_seconds_total", "node_memory_free_bytes", "up"]
    r = await execute("label_values", {"label": "__name__", "contains": "cpu"}, ctx)
    assert r["result"]["values"] == ["node_cpu_seconds_total"]


async def test_targets_health_cuenta_y_detalla(ctx, prom):
    prom.targets = [
        {"labels": {"job": "node", "instance": "a:9100"}, "health": "up"},
        {"labels": {"job": "node", "instance": "b:9100"}, "health": "down",
         "lastError": "connection refused"},
    ]
    r = await execute("targets_health", {"only_down": True}, ctx)
    res = r["result"]
    assert (res["total"], res["up"], res["down"]) == (2, 1, 1)
    assert len(res["targets"]) == 1
    assert res["targets"][0]["instance"] == "b:9100"


async def test_sin_backend_de_costos_la_tool_lo_explica(ctx):
    """El modelo puede transmitir esto al usuario. Explotar, no."""
    r = await execute("cost_daily", {}, ctx)
    assert r["status"] == "error"
    assert "backend de costos" in r["result"]


def test_las_tools_de_costo_no_se_le_ofrecen_al_modelo_si_no_hay_costos(ctx):
    """Ofrecérselas garantiza que las pida, falle, y gaste un paso del
    presupuesto en descubrir que no existen."""
    from copilot.agent.registry import definitions

    nombres = {d["function"]["name"] for d in definitions(ctx)}
    assert "promql_instant" in nombres
    assert not any(n.startswith("cost_") for n in nombres)
