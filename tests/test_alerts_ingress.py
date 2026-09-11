"""El ingreso de alertas: parsear el webhook, decidir antes de gastar,
enriquecer sin que nada frene la entrega, y responder rápido."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from copilot.adapters.notify_log import LogNotifier
from copilot.agent.registry import Context, execute
from copilot.alerts import alertmanager
from copilot.alerts.ingress import to_message
from copilot.api.app import create_app
from copilot.dispatch import Dispatcher, Policy
from copilot.ports.alerts import SignalStatus
from copilot.ports.notify import Severity
from copilot.runtime import Runtime
from tests.conftest import FakeModel, matrix, rule

AUTH = {"Authorization": "Bearer test-token"}


def webhook(*alerts: dict, status: str = "firing") -> dict:
    """Un payload v4 como lo manda Alertmanager, con nanosegundos y todo."""
    return {
        "version": "4", "status": status, "receiver": "copilot",
        "externalURL": "http://am:9093",
        "commonLabels": {"env": "prod"},
        "alerts": [{
            "status": a.get("status", status),
            "labels": {"alertname": a["name"], "severity": a.get("severity", "warning"),
                       **a.get("labels", {})},
            "annotations": {"summary": a.get("summary", "")},
            "startsAt": "2026-09-11T10:00:00.123456789Z",
            "endsAt": a.get("endsAt", "0001-01-01T00:00:00Z"),
            "fingerprint": a["fp"],
        } for a in alerts],
    }


# --- Parseo ------------------------------------------------------------------


def test_parsea_el_webhook_v4_con_labels_comunes_y_nanosegundos():
    señales, rechazos = alertmanager.parse(webhook({"name": "HighCPU", "fp": "f1",
                                                     "labels": {"instance": "a:9100"}}))
    assert rechazos == []
    s = señales[0]
    assert (s.name, s.fingerprint, s.status) == ("HighCPU", "f1", SignalStatus.FIRING)
    assert s.labels == {"env": "prod", "alertname": "HighCPU", "severity": "warning",
                        "instance": "a:9100"}
    assert s.starts_at.year == 2026 and s.ends_at is None
    assert s.source == "http://am:9093"


def test_una_alerta_rota_no_tira_el_lote():
    payload = webhook({"name": "A", "fp": "f1"})
    payload["alerts"].append({"labels": {}, "fingerprint": ""})
    payload["alerts"].append("basura")
    señales, rechazos = alertmanager.parse(payload)
    assert [s.name for s in señales] == ["A"]
    assert len(rechazos) == 2


def test_un_payload_que_no_es_de_alertmanager_lo_dice():
    _, rechazos = alertmanager.parse({"hola": 1})
    assert "webhook de Alertmanager" in rechazos[0]


# --- Mensaje -----------------------------------------------------------------


def test_el_mensaje_de_una_resuelta_no_lleva_enriquecimiento_y_cierra_el_dedup():
    s, _ = alertmanager.parse(webhook({"name": "A", "fp": "f1", "status": "resolved",
                                       "endsAt": "2026-09-11T11:00:00Z"}))
    m = to_message(s[0], None)
    assert m.severity is Severity.RESOLVED and m.title.startswith("[RESUELTA]")
    assert "hasta 2026-09-11T11:00" in m.body and m.fingerprint == "f1"


# --- Flujo completo -----------------------------------------------------------


@pytest.fixture
def rt(config, metrics_port, prom):
    """Runtime con Prometheus falso, canal de log y un modelo con guion de triage."""
    prom.rule_groups = [{"name": "g", "rules": [rule("TargetCaido", "up == 0", state="firing")]}]
    prom.alerts = []
    prom.series["up == 0"] = [matrix({"job": "caido", "instance": "x"},
                                     [(1700000000, 1.0), (1700000060, 1.0)])]
    modelo = FakeModel([FakeModel.say("El target x del job caido no resuelve DNS.")])
    canal = LogNotifier(name="ops")
    rt = Runtime(config=config, metrics=metrics_port, model=modelo, notify=[canal],
                 dispatcher=Dispatcher([canal], Policy()))
    rt.canal, rt.modelo = canal, modelo
    return rt


async def test_una_firing_se_enriquece_con_la_regla_y_el_triage_y_se_entrega(rt):
    r = await rt.ingress.receive(webhook({"name": "TargetCaido", "fp": "f1",
                                          "labels": {"job": "caido", "instance": "x"}}))
    assert r["queued"] == ["f1"] and r["skipped"] == []
    await rt.ingress.drain()

    rec = rt.alert_log.recent()[0]
    assert rec.decision == "sent"
    assert rec.enrichment["expression"] == "up == 0"
    assert rec.enrichment["trend"][0]["series"].startswith("{instance")
    assert rec.enrichment["triage"].startswith("El target x")
    m = rt.canal.sent[-1]
    assert m.title == "[WARNING] TargetCaido — x"
    assert "Regla: up == 0" in m.body and "no resuelve DNS" in m.body
    assert m.fingerprint == "f1"
    # El modelo recibió la expresión y la tendencia, sin tener que buscarlas.
    pregunta = rt.modelo.seen[0][-1]["content"]
    assert "up == 0" in pregunta and "seis líneas" in pregunta


async def test_la_repetida_se_deduplica_antes_de_gastar_un_token(rt):
    hook = webhook({"name": "TargetCaido", "fp": "f1"})
    await rt.ingress.receive(hook)
    await rt.ingress.drain()
    r = await rt.ingress.receive(hook)
    assert r["queued"] == [] and r["skipped"] == [{"fingerprint": "f1", "decision": "deduped"}]
    assert len(rt.modelo.seen) == 1


async def test_sin_modelo_la_alerta_sale_igual_pelada(rt):
    rt.model = None
    await rt.ingress.receive(webhook({"name": "TargetCaido", "fp": "f2"}))
    await rt.ingress.drain()
    rec = rt.alert_log.recent()[0]
    assert rec.decision == "sent" and rec.enrichment["triage"] == ""
    assert "Regla: up == 0" in rt.canal.sent[-1].body


async def test_si_el_backend_explota_la_alerta_sale_igual(rt, prom):
    """Una alerta que no llega porque el enriquecimiento falló es peor que
    una alerta pelada."""
    prom.fail_with = 500
    await rt.ingress.receive(webhook({"name": "TargetCaido", "fp": "f3"}))
    await rt.ingress.drain()
    rec = rt.alert_log.recent()[0]
    assert rec.decision == "sent" and rec.enrichment["expression"] == ""
    assert any(e.startswith("activas") for e in rec.enrichment["errors"])
    assert rt.canal.sent[-1].title.startswith("[WARNING] TargetCaido")


async def test_una_resuelta_no_pasa_por_el_modelo(rt):
    await rt.ingress.receive(webhook({"name": "TargetCaido", "fp": "f4", "status": "resolved"}))
    await rt.ingress.drain()
    assert rt.modelo.seen == []
    assert rt.canal.sent[-1].severity is Severity.RESOLVED


async def test_alerts_received_es_lo_que_sono(rt):
    await rt.ingress.receive(webhook({"name": "TargetCaido", "fp": "f5"}))
    await rt.ingress.drain()
    r = await execute("alerts_received", {"hours": 1}, rt.context())
    assert r["status"] == "ok"
    fila = r["result"]["alerts"][0]
    assert fila["name"] == "TargetCaido" and fila["decision"] == "sent"
    assert fila["triage"].startswith("El target")


async def test_sin_ingreso_la_tool_lo_dice():
    r = await execute("alerts_received", {}, Context())
    assert r["status"] == "error" and "Alertmanager" in r["result"]


# --- API ---------------------------------------------------------------------


@pytest.fixture
def client(config, metrics_port, model, monkeypatch):
    canal = LogNotifier(name="ops")

    def _build(cfg):
        return Runtime(config=cfg, metrics=metrics_port, model=model, notify=[canal],
                       dispatcher=Dispatcher([canal]))

    monkeypatch.setattr("copilot.api.app.build", _build)
    with TestClient(create_app(config)) as c:
        c.canal = canal
        yield c


def test_post_alerts_contesta_202_enseguida_y_exige_token(client):
    assert client.post("/v1/alerts", json=webhook({"name": "A", "fp": "f1"})).status_code == 401
    r = client.post("/v1/alerts", headers=AUTH, json=webhook({"name": "A", "fp": "f1"}))
    assert r.status_code == 202
    assert r.json()["queued"] == ["f1"]
    listado = client.get("/v1/alerts", headers=AUTH).json()
    assert listado["alerts"][0]["fingerprint"] == "f1"
