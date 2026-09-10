"""El criterio de salida de F0, escrito como test.

    «docker run + un Prometheus de juguete → /v1/chat contesta cuántos targets
    están up.»

Acá se corre la misma cadena completa —HTTP → auth → agente → presupuesto →
adapter → parseo de la API v1 → respuesta con traza— con el transporte falseado
en el último salto, así que verifica todo salvo la red. La versión con
contenedores de verdad es `scripts/verify-f0.sh`.

El modelo del test se comporta como uno de verdad: primero descubre qué hay,
después consulta, y recién entonces contesta usando lo que volvió. Un guion que
saltee el descubrimiento probaría menos de lo que parece.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from copilot.api.app import create_app
from copilot.ports.model import ModelReply

AUTH = {"Authorization": "Bearer test-token"}

# Un Prometheus de juguete real: se scrapea a sí mismo, scrapea al copiloto, y
# tiene un target caído a propósito — sin algo caído, "cuántos están up" se
# contesta con un número y no se ve si el agente sabe mirar el detalle.
TARGETS = [
    {"labels": {"job": "prometheus", "instance": "localhost:9090"}, "health": "up"},
    {"labels": {"job": "copilot", "instance": "copilot:8080"}, "health": "up"},
    {"labels": {"job": "caido_a_proposito", "instance": "no-existe.invalid:9100"},
     "health": "down", "lastError": "dial tcp: lookup no-existe.invalid: no such host"},
]


class ModeloQueDescubre:
    """Hace lo que hace un modelo bien prompteado: mira, consulta, contesta."""

    name = "guionado"
    default_model = "guionado-1"

    def __init__(self):
        self.turnos = 0
        self.visto: list[dict] = []

    async def complete(self, messages, *, tools=None, model=None, temperature=0.2):
        self.turnos += 1
        disponibles = {t["function"]["name"] for t in (tools or [])}

        if self.turnos == 1:
            assert "targets_health" in disponibles, "la tool no llegó al catálogo del turno"
            return ModelReply(tool_calls=[{
                "id": "t1", "type": "function",
                "function": {"name": "targets_health", "arguments": "{}"},
            }], tokens=42, model=self.default_model)

        # El resultado de la tool tiene que estar en el historial, o el modelo
        # no tendría con qué contestar.
        payload = json.loads(messages[-1]["content"])
        self.visto.append(payload)
        caidos = ", ".join(t["job"] for t in payload["targets"] if t["health"] != "up")
        return ModelReply(
            content=(f"Hay {payload['up']} de {payload['total']} targets arriba. "
                     f"El que está caído es {caidos}."),
            tokens=58, model=self.default_model)

    async def check(self) -> None:
        return


@pytest.fixture
def app_completa(config, metrics_port, prom, monkeypatch):
    prom.targets = TARGETS
    modelo = ModeloQueDescubre()

    from copilot import runtime as runtime_mod

    monkeypatch.setattr(
        "copilot.api.app.build",
        lambda cfg: runtime_mod.Runtime(config=cfg, metrics=metrics_port, model=modelo))
    with TestClient(create_app(config)) as c:
        yield c, modelo, prom


def test_el_criterio_de_salida_de_f0(app_completa):
    client, modelo, prom = app_completa

    r = client.post("/v1/chat", headers=AUTH,
                    json={"message": "¿cuántos targets están up?"})
    assert r.status_code == 200
    d = r.json()

    # 1. Contestó con el número real, no con uno inventado.
    assert "2 de 3" in d["text"]
    assert "caido_a_proposito" in d["text"]

    # 2. Llegó ahí usando una tool, y la traza lo demuestra.
    assert [s["tool"] for s in d["steps"]] == ["targets_health"]
    assert d["steps"][0]["ok"] is True

    # 3. El dato salió de la API v1 de verdad, parseada por el adapter real.
    assert ("/api/v1/targets", {"state": "active"}) in prom.calls
    assert modelo.visto[0]["up"] == 2

    # 4. Quedó registrado qué se consultó contra los datos del cliente.
    auditoria = client.get("/v1/audit", headers=AUTH).json()
    assert auditoria["entries"][0]["tool"] == "targets_health"

    # 5. Y se contabilizó, porque esto también es un target de Prometheus.
    metricas = client.get("/metrics").text
    assert 'copilot_agent_steps_total{ok="true",tool="targets_health"}' in metricas
    assert 'copilot_agent_runs_total{status="done"}' in metricas


def test_sin_modelo_configurado_el_chat_lo_dice_en_vez_de_explotar(config, metrics_port,
                                                                   monkeypatch):
    from copilot import runtime as runtime_mod

    monkeypatch.setattr(
        "copilot.api.app.build",
        lambda cfg: runtime_mod.Runtime(config=cfg, metrics=metrics_port, model=None))
    with TestClient(create_app(config)) as c:
        r = c.post("/v1/chat", headers=AUTH, json={"message": "hola"})
    assert r.status_code == 503
    assert "ModelPort" in r.json()["detail"]
