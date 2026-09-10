"""Dobles de prueba: un Prometheus falso y un modelo con guion.

El Prometheus falso es un `MockTransport` de httpx y no un mock del adapter. La
diferencia importa: así los tests corren el parseo real —resultados vector y
matrix, valores como string, NaN, el `scalar` que viene con otra forma— que es
donde de verdad se rompen estas cosas. Un mock del adapter sólo probaría que el
mock devuelve lo que le pusimos.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from copilot.config import Config
from copilot.ports.model import ModelReply

# --- Un Prometheus que se puede guionar ------------------------------------


@dataclass
class FakePrometheus:
    """Sirve la HTTP API v1 con datos fijos. Registra lo que le preguntaron."""

    series: dict[str, list[dict]] = field(default_factory=dict)
    targets: list[dict] = field(default_factory=list)
    labels: dict[str, list[str]] = field(default_factory=dict)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    fail_with: int | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.calls.append((path, params))

        if self.fail_with:
            return httpx.Response(self.fail_with, json={
                "status": "error", "errorType": "bad_data", "error": "explotó a propósito"})

        if path == "/api/v1/query":
            q = params.get("query", "")
            if q == "1":
                return self._ok({"resultType": "scalar", "result": [1700000000, "1"]})
            return self._ok({"resultType": "vector",
                             "result": self.series.get(q, [])})

        if path == "/api/v1/query_range":
            q = params.get("query", "")
            return self._ok({"resultType": "matrix", "result": self.series.get(q, [])})

        if path.startswith("/api/v1/label/") and path.endswith("/values"):
            nombre = path.split("/")[4]
            return self._ok(self.labels.get(nombre, []))

        if path == "/api/v1/targets":
            return self._ok({"activeTargets": self.targets})

        return httpx.Response(404, json={"status": "error", "error": f"sin ruta: {path}"})

    @staticmethod
    def _ok(data: Any) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": data})


def vector(labels: dict[str, str], value: float, ts: float = 1700000000.0) -> dict:
    return {"metric": labels, "value": [ts, str(value)]}


def matrix(labels: dict[str, str], values: list[tuple[float, float]]) -> dict:
    return {"metric": labels, "values": [[t, str(v)] for t, v in values]}


@pytest.fixture
def prom() -> FakePrometheus:
    return FakePrometheus()


@pytest.fixture
def metrics_port(prom: FakePrometheus):
    from copilot.adapters.metrics_prometheus import PrometheusMetrics

    return PrometheusMetrics(url="http://prom.test:9090", transport=prom.transport())


# --- Un modelo con guion ---------------------------------------------------


class FakeModel:
    """Devuelve respuestas preparadas, en orden.

    Cada entrada del guion puede ser un `ModelReply` o una función que recibe el
    historial: eso permite escribir un test donde la respuesta final depende de
    lo que devolvió la tool, que es lo único que prueba que el resultado llegó
    de verdad al modelo y no se perdió en el armado del historial.
    """

    name = "fake"
    default_model = "fake-model"

    def __init__(self, script: list[Any] | None = None):
        self.script = list(script or [])
        self.seen: list[list[dict]] = []
        self.tools_offered: list[list[str]] = []
        self.checked = False

    async def complete(self, messages, *, tools=None, model=None, temperature=0.2):
        self.seen.append(list(messages))
        self.tools_offered.append([t["function"]["name"] for t in (tools or [])])
        if not self.script:
            return ModelReply(content="(sin guion)", tokens=1, model=self.default_model)
        siguiente = self.script.pop(0)
        r = siguiente(messages) if callable(siguiente) else siguiente
        return r

    async def check(self) -> None:
        self.checked = True

    # --- helpers para escribir guiones ---
    @staticmethod
    def call(tool: str, args: dict | None = None, call_id: str = "c1") -> ModelReply:
        return ModelReply(
            tool_calls=[{
                "id": call_id, "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args or {})},
            }],
            tokens=10, model="fake-model",
        )

    @staticmethod
    def say(text: str) -> ModelReply:
        return ModelReply(content=text, tokens=5, model="fake-model")


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


# --- Config y runtime ------------------------------------------------------


@pytest.fixture
def config() -> Config:
    return Config.from_dict({
        "server": {"api_token": "test-token"},
        "metrics": {"adapter": "prometheus", "url": "http://prom.test:9090"},
        "model": {"adapter": "openai_compat", "base_url": "http://model.test/v1",
                  "model": "fake-model"},
        "agent": {"max_steps": 4, "max_seconds": "10s"},
        "notify": [{"name": "ops", "adapter": "log"}],
    })


@pytest.fixture
def runtime(config, metrics_port, model):
    """Runtime armado a mano con los dobles ya enchufados."""
    from copilot.runtime import Runtime

    return Runtime(config=config, metrics=metrics_port, model=model)


@pytest.fixture(autouse=True)
def _clean_audit():
    from copilot.agent import audit

    audit.reset()
    yield
    audit.reset()
