"""El borde HTTP: auth, salud y el contrato de /v1/chat."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from copilot.api.app import create_app
from tests.conftest import FakeModel, vector

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(config, metrics_port, model, monkeypatch):
    """App real, puertos falsos: se arma el runtime a mano en vez de dejar que
    el lifespan instancie adapters contra servicios que no existen."""
    from copilot import runtime as runtime_mod

    def _build(cfg):
        return runtime_mod.Runtime(config=cfg, metrics=metrics_port, model=model)

    monkeypatch.setattr("copilot.api.app.build", _build)
    with TestClient(create_app(config)) as c:
        yield c


# --- Salud ------------------------------------------------------------------


def test_healthz_no_toca_ninguna_dependencia(client, prom):
    """Es la liveness probe. Si respondiera por el Prometheus del cliente,
    Kubernetes reiniciaría el pod justo cuando ese Prometheus está lento — es
    decir, cuando más falta hace el agente."""
    prom.fail_with = 500
    assert client.get("/healthz").status_code == 200


def test_readyz_saca_del_balanceador_si_el_backend_no_responde(client, prom):
    assert client.get("/readyz").json()["ready"] is True
    prom.fail_with = 503
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["ready"] is False
    assert "metrics" in r.json()["ports"]


def test_readyz_tolera_que_falten_los_puertos_opcionales(client):
    """Sin costos configurados se puede contestar sobre métricas igual."""
    cuerpo = client.get("/readyz").json()
    assert cuerpo["ports"]["cost"] == "not configured"
    assert cuerpo["ready"] is True


def test_las_metricas_del_copiloto_salen_en_formato_prometheus(client):
    """Se instala en el stack de un equipo de SRE: lo primero que van a querer
    es un target más en su Prometheus."""
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "copilot_http_requests_total" in r.text


# --- Auth -------------------------------------------------------------------


def test_las_rutas_de_datos_piden_token(client):
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers=AUTH).status_code == 200


def test_las_rutas_de_salud_quedan_fuera_del_token(client):
    """Si el kubelet tuviera que autenticarse, un token mal puesto se vería como
    un CrashLoop sin explicación."""
    for ruta in ("/healthz", "/readyz", "/metrics"):
        assert client.get(ruta).status_code in (200, 503)


def test_un_token_equivocado_no_alcanza(client):
    r = client.get("/v1/status", headers={"Authorization": "Bearer otro"})
    assert r.status_code == 401
    assert "Bearer" in r.json()["detail"]


# --- Estado y catálogo ------------------------------------------------------


def test_status_dice_que_quedo_enchufado_y_con_que_topes(client):
    d = client.get("/v1/status", headers=AUTH).json()
    assert d["ports"]["metrics"] == "prometheus"
    assert d["budget"]["max_steps"] == 4
    assert set(d["permissions"].values()) == {"read"}


def test_el_catalogo_muestra_lo_que_ve_el_modelo(client):
    """Sirve para depurar por qué no usó una tool: casi siempre es que no estaba
    en el catálogo de ese turno."""
    d = client.get("/v1/tools", headers=AUTH).json()
    nombres = {t["function"]["name"] for t in d["tools"]}
    assert "promql_instant" in nombres and "targets_health" in nombres


# --- Chat -------------------------------------------------------------------


def test_el_chat_devuelve_la_traza_junto_con_la_respuesta(client, model, prom):
    """Un agente de observabilidad que contesta un número sin mostrar de dónde
    salió es un agente en el que nadie confía la segunda vez."""
    prom.series["up"] = [vector({"job": "node"}, 1.0)]
    model.script = [
        FakeModel.call("promql_instant", {"query": "up"}),
        FakeModel.say("Hay 1 target arriba: job=node."),
    ]
    r = client.post("/v1/chat", headers=AUTH, json={"message": "¿está todo arriba?"})
    assert r.status_code == 200
    d = r.json()
    assert d["text"].startswith("Hay 1 target")
    assert [s["tool"] for s in d["steps"]] == ["promql_instant"]
    assert d["steps"][0]["args"] == {"query": "up"}


def test_lo_consultado_queda_en_la_auditoria(client, model, prom):
    prom.series["up"] = [vector({"job": "node"}, 1.0)]
    model.script = [FakeModel.call("promql_instant", {"query": "up"}),
                    FakeModel.say("ok")]
    client.post("/v1/chat", headers=AUTH,
                json={"message": "?", "actor": "ana@cliente.com"})

    d = client.get("/v1/audit", headers=AUTH).json()
    assert d["summary"]["entries"] == 1
    fila = d["entries"][0]
    assert fila["actor"] == "ana@cliente.com"
    assert fila["args"] == {"query": "up"}
    assert d["summary"]["durable"] is False


def test_un_mensaje_vacio_se_rechaza_en_el_borde(client):
    assert client.post("/v1/chat", headers=AUTH, json={"message": ""}).status_code == 422
