"""Observabilidad del propio copiloto.

Un producto que monitorea tiene que ser monitoreable, y no por simetría poética:
se instala en el stack de un equipo de SRE, y lo primero que van a querer es un
target más en su Prometheus. Que las métricas salgan en `/metrics` con el
formato de siempre significa que lo scrapean sin preguntarnos nada.
"""
from .metrics import (
    AGENT_RUNS,
    AGENT_STEPS,
    AGENT_TOKENS,
    DETECTOR_RUNS,
    HTTP_DURATION,
    HTTP_REQUESTS,
    NOTIFY_DELIVERIES,
    NOTIFY_OUTCOMES,
    PORT_CHECKS,
    REGISTRY,
    TOOL_LATENCY,
    render,
)

__all__ = [
    "AGENT_RUNS", "AGENT_STEPS", "AGENT_TOKENS", "DETECTOR_RUNS", "HTTP_DURATION",
    "HTTP_REQUESTS", "NOTIFY_DELIVERIES", "NOTIFY_OUTCOMES", "PORT_CHECKS", "REGISTRY",
    "TOOL_LATENCY", "render",
]
