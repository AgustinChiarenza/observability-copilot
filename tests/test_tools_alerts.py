"""Las tools de alertas: qué está en rojo, y la expresión que lo explica."""
from __future__ import annotations

import pytest

from copilot.agent.registry import Context, execute
from tests.conftest import alert, rule


@pytest.fixture
def ctx(metrics_port):
    return Context(metrics=metrics_port)


async def test_el_adapter_parsea_alertas_con_nanosegundos(metrics_port, prom):
    """Prometheus manda activeAt con nueve decimales; fromisoformat acepta
    seis. Si esto rompe, ninguna alerta tiene fecha."""
    prom.alerts = [alert("HighCPU", instance="a:9100", severity="critical")]
    r = await metrics_port.alerts()
    assert r[0].name == "HighCPU"
    assert r[0].severity == "critical"
    assert r[0].active_at is not None and r[0].active_at.year == 2026


async def test_el_adapter_deja_afuera_las_recording_rules(metrics_port, prom):
    prom.rule_groups = [{"name": "g", "rules": [
        rule("HighCPU", "cpu > 0.9"),
        {"type": "recording", "name": "job:up:sum", "query": "sum(up)"},
    ]}]
    r = await metrics_port.rules()
    assert [x.name for x in r] == ["HighCPU"]
    assert r[0].expression == "cpu > 0.9"
    assert r[0].duration_s == 300
    _, params = prom.calls[-1]
    assert params["type"] == "alert"


async def test_por_defecto_solo_las_firing_y_cuenta_por_nombre(ctx, prom):
    prom.alerts = [
        alert("HighCPU", instance="a"), alert("HighCPU", instance="b"),
        alert("DiskFull", state="pending", instance="c"),
    ]
    r = await execute("alerts_active", {}, ctx)
    res = r["result"]
    assert (res["total"], res["firing"], res["pending"]) == (3, 2, 1)
    assert res["shown"] == 2
    assert res["by_name"] == {"HighCPU": 2}
    assert "alertname" not in res["alerts"][0]["labels"]
    assert res["alerts"][0]["summary"] == "HighCPU resumen"


async def test_state_all_y_contains(ctx, prom):
    prom.alerts = [alert("HighCPU"), alert("DiskFull", state="pending")]
    r = await execute("alerts_active", {"state": "all", "contains": "disk"}, ctx)
    assert [a["name"] for a in r["result"]["alerts"]] == ["DiskFull"]


async def test_las_reglas_traen_la_expresion_y_avisan_las_rotas(ctx, prom):
    """Una regla con health err no dispara aunque el sistema esté en llamas: es
    la alerta que falta, y hay que decirlo."""
    prom.rule_groups = [{"name": "node", "rules": [
        rule("HighCPU", "cpu > 0.9", state="firing", alerts=2),
        rule("Rota", "metrica_que_no_existe >", health="err", lastError="parse error"),
    ]}]
    r = await execute("alert_rules", {}, ctx)
    reglas = {x["name"]: x for x in r["result"]["rules"]}
    assert reglas["HighCPU"]["expression"] == "cpu > 0.9"
    assert reglas["HighCPU"]["active"] == 2
    assert reglas["Rota"]["health"] == "err"
    assert "parse error" in reglas["Rota"]["last_error"]
    assert "health" not in reglas["HighCPU"]


async def test_only_active_filtra_las_inactivas(ctx, prom):
    prom.rule_groups = [{"name": "g", "rules": [
        rule("A", "x", state="firing", alerts=1), rule("B", "y"),
    ]}]
    r = await execute("alert_rules", {"only_active": True}, ctx)
    assert [x["name"] for x in r["result"]["rules"]] == ["A"]


async def test_sin_backend_de_metricas_lo_dice(prom):
    r = await execute("alerts_active", {}, Context())
    assert r["status"] == "error"
    assert "métricas" in r["result"]
