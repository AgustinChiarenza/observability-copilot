"""Métricas Prometheus del copiloto.

Registro propio (`CollectorRegistry`) en vez del global: el global junta lo que
registre cualquier librería que alguien importe, y esto se expone en la red del
cliente. Lo que sale de `/metrics` tiene que ser exactamente lo que decidimos
publicar.

Todos los contadores del agente llevan el label que hace falta para agrupar:
`agent_tokens_total` por modelo, `agent_steps_total` por tool. Se aprendió a los
golpes en el proyecto anterior — una métrica con labels no tiene ninguna muestra
sin labels, así que un panel (o un test) que la busque sin agrupar lee cero para
siempre y nadie se entera.
"""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "copilot_http_requests_total", "Requests HTTP atendidos",
    ["method", "path", "status"], registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "copilot_http_request_duration_seconds", "Duración de los requests HTTP",
    ["method", "path"], registry=REGISTRY,
)

AGENT_RUNS = Counter(
    "copilot_agent_runs_total", "Turnos del agente terminados",
    ["status"], registry=REGISTRY,   # done | budget | time | error
)
AGENT_STEPS = Counter(
    "copilot_agent_steps_total", "Tools ejecutadas por el agente",
    ["tool", "ok"], registry=REGISTRY,
)
AGENT_TOKENS = Counter(
    "copilot_agent_tokens_total", "Tokens consumidos por el agente",
    ["model"], registry=REGISTRY,
)
TOOL_LATENCY = Histogram(
    "copilot_tool_duration_seconds", "Duración de cada tool",
    ["tool"], registry=REGISTRY,
    # Una tool va de milisegundos (un label_values cacheado) a decenas de
    # segundos (un range grande contra un Thanos con object storage atrás). Los
    # buckets por defecto llegan a 10s y aplastarían todo lo interesante.
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 80),
)

NOTIFY_OUTCOMES = Counter(
    "copilot_notify_outcomes_total", "Qué decidió el despachante por cada mensaje",
    ["decision"], registry=REGISTRY,   # sent | deduped | quiet | capped | no_channels
)
NOTIFY_DELIVERIES = Counter(
    "copilot_notify_deliveries_total", "Entregas por canal",
    ["channel", "ok"], registry=REGISTRY,   # ok: true | false | capped
)
DETECTOR_RUNS = Counter(
    "copilot_detector_runs_total", "Corridas de cada detector",
    ["detector", "outcome"], registry=REGISTRY,   # spike | clear | no_data | error
)

PORT_CHECKS = Gauge(
    "copilot_port_up", "1 si el puerto respondió al último chequeo",
    ["port"], registry=REGISTRY,
)


def render() -> bytes:
    return generate_latest(REGISTRY)
