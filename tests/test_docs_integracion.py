"""La guía de integración promete formas de respuesta; esto verifica que las
claves que documenta sean las que la API devuelve. Si alguien renombra un
campo, falla acá y no en el bot del cliente."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copilot.api.app import create_app

GUIA = Path(__file__).resolve().parents[1] / "docs" / "integracion.md"


@pytest.fixture
def client(config, metrics_port, model, monkeypatch):
    from copilot import runtime as runtime_mod

    monkeypatch.setattr("copilot.api.app.build",
                        lambda cfg: runtime_mod.Runtime(config=cfg, metrics=metrics_port, model=model))
    with TestClient(create_app(config), headers={"Authorization": "Bearer test-token"}) as c:
        yield c


def _claves_documentadas(seccion: str) -> set[str]:
    texto = GUIA.read_text()
    inicio = texto.index(seccion)
    bloque = texto[inicio:].split("```json", 1)[1].split("```", 1)[0]
    return set(re.findall(r'^\s{2}"([a-z_]+)":', bloque, re.MULTILINE))


def test_status_tiene_las_claves_de_la_guia(client):
    r = client.get("/v1/status")
    assert r.status_code == 200
    assert _claves_documentadas("`/v1/status`:") == set(r.json())


def test_chat_tiene_las_claves_de_la_guia(client):
    r = client.post("/v1/chat", json={"message": "hola", "actor": "test"})
    assert r.status_code == 200
    assert _claves_documentadas("### Response (200)") == set(r.json())


def test_alerts_y_audit_tienen_las_claves_de_la_guia(client):
    alertas = client.get("/v1/alerts").json()
    assert _claves_documentadas("### `GET /v1/alerts`") == set(alertas)
    auditoria = client.get("/v1/audit").json()
    assert _claves_documentadas("## 6. Auditoría") == set(auditoria)


def test_decisiones_documentadas_son_las_del_codigo():
    from copilot import dispatch
    from copilot.alerts import ingress
    texto = GUIA.read_text()
    tabla = texto[texto.index("### 4.3"):texto.index("## 5.")]
    documentadas = set(re.findall(r"^\| `([a-z_]+)` \|", tabla, re.MULTILINE)) - {"decision"}
    del_despachante = set(re.findall(r"(\w+) \|", dispatch.Outcome.__doc__)) | {"unpaired"}
    del_ingreso = set(re.findall(r'decision = "(\w+)"', Path(ingress.__file__).read_text()))
    assert documentadas == del_despachante | del_ingreso | {"pending"}


def test_receiver_contesta_lo_que_dice_la_guia(client):
    payload = {"version": "4", "status": "firing", "alerts": [
        {"labels": {"alertname": "X", "severity": "warning"}, "fingerprint": "abc",
         "status": "firing", "startsAt": "2026-09-14T00:00:00Z", "annotations": {}},
        "esto no es un objeto",
    ]}
    r = client.post("/v1/alerts", json=payload)
    assert r.status_code == 202
    assert _claves_documentadas("Response 202:") == set(r.json())
    assert r.json()["rejected"] and r.json()["received"] == 1
