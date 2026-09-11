"""Las rutas versionadas.

`/v1` desde el primer día porque esto se instala en el stack de otro: cuando
cambie el contrato, el cliente tiene que poder seguir con el viejo mientras
migra su bot y sus receivers. Romper una URL sin versión obliga a coordinar un
deploy ajeno, y eso no se hace desde acá.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import service, telemetry
from ..agent import audit, registry
from ..agent.permissions import classified
from ..detectors import cost_spike
from ..ports.notify import Message, Severity
from ..runtime import Runtime

logger = logging.getLogger(__name__)

router = APIRouter()


def _rt(request: Request) -> Runtime:
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:  # pragma: no cover — sólo si alguien saltea el lifespan
        raise HTTPException(503, "El runtime no está armado.")
    return rt


# --- Salud y estado ---------------------------------------------------------


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    estado = await _rt(request).check()
    for puerto, r in estado.items():
        telemetry.PORT_CHECKS.labels(port=puerto).set(1 if r == "ok" else 0)
    # El costo y los canales pueden estar rotos sin que el producto deje de
    # servir: se puede contestar sobre métricas igual. Métricas y modelo no —
    # sin alguno de esos dos no hay nada que ofrecer.
    esenciales = {k: v for k, v in estado.items() if k in ("metrics", "model")}
    listo = all(v == "ok" for v in esenciales.values())
    return JSONResponse(status_code=200 if listo else 503,
                        content={"ready": listo, "ports": estado})


@router.get("/v1/status")
async def status(request: Request) -> dict[str, Any]:
    """Qué quedó enchufado y qué puede hacer el agente."""
    rt = _rt(request)
    return {
        "version": "0.1.0",
        "ports": rt.describe(),
        "tools": sorted(classified()),
        "permissions": classified(),
        "budget": {
            "max_steps": rt.config.agent.max_steps,
            "max_seconds": rt.config.agent.max_seconds,
            "max_series": rt.config.budget.max_series,
            "max_points": rt.config.budget.max_points,
            "max_range_days": rt.config.budget.max_range.days,
        },
        "detectors": {
            cost_spike.NAME: {
                "enabled": rt.config.detectors.cost_spike.enabled and rt.cost is not None,
                "interval_s": rt.config.detectors.cost_spike.interval.total_seconds(),
                "threshold": rt.config.detectors.cost_spike.threshold,
                "window_days": rt.config.detectors.cost_spike.window_days,
            },
        },
        "storage": {
            "durable": rt.storage.durable,
            "path": str(rt.storage.root) if rt.storage.root else None,
        },
    }


# --- Chat -------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8_000)
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    actor: str = Field(default="anonymous", max_length=120)


class ChatStep(BaseModel):
    tool: str
    args: dict[str, Any]
    ok: bool
    ms: int


class ChatResponse(BaseModel):
    text: str
    steps: list[ChatStep]
    tokens: int
    model: str
    stopped_by: str = ""


@router.post("/v1/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """Una pregunta sobre los datos del cliente, contestada con tools de lectura.

    La traza sale en la respuesta y no sólo en el log: quien pregunta tiene que
    poder ver qué queries se corrieron para llegar a eso. Un agente de
    observabilidad que contesta un número sin mostrar de dónde salió es un
    agente en el que nadie va a confiar la segunda vez.
    """
    rt = _rt(request)
    if rt.model is None:
        raise HTTPException(503, "No hay ModelPort configurado.")

    try:
        r = await service.answer(rt, body.message, history=body.history, actor=body.actor)
    except Exception as e:
        telemetry.AGENT_RUNS.labels(status="error").inc()
        logger.exception("chat: turno fallido")
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e

    telemetry.AGENT_RUNS.labels(status=r.stopped_by or "done").inc()
    telemetry.AGENT_TOKENS.labels(model=r.model or "unknown").inc(r.tokens)
    for paso in r.steps:
        telemetry.AGENT_STEPS.labels(tool=paso.tool, ok=str(paso.ok).lower()).inc()
        telemetry.TOOL_LATENCY.labels(tool=paso.tool).observe(paso.ms / 1000)

    return ChatResponse(
        text=r.text,
        steps=[ChatStep(tool=s.tool, args=s.args, ok=s.ok, ms=s.ms) for s in r.steps],
        tokens=r.tokens, model=r.model, stopped_by=r.stopped_by,
    )


# --- Tools y auditoría ------------------------------------------------------


@router.get("/v1/tools")
async def tools(request: Request) -> dict[str, Any]:
    """El catálogo tal como lo ve el modelo. Sirve para depurar por qué no usó
    una tool: casi siempre es que no estaba en el catálogo de ese turno."""
    ctx = _rt(request).context()
    definiciones = registry.definitions(ctx)
    return {"count": len(definiciones), "tools": definiciones}


@router.get("/v1/audit")
async def audit_timeline(
    request: Request, limit: int = 100, tool: str = "", only_errors: bool = False,
) -> dict[str, Any]:
    """Qué se consultó contra los datos del cliente."""
    _rt(request)
    return {
        "summary": audit.summary(),
        "note": "Buffer en memoria: se pierde al reiniciar. La persistencia es de F7.",
        "entries": audit.timeline(limit=min(limit, 500), tool=tool, only_errors=only_errors),
    }


# --- Notificaciones y detectores -------------------------------------------


class NotifyTestRequest(BaseModel):
    title: str = Field(default="Copilot: mensaje de prueba", max_length=200)
    body: str = Field(default="Si estás leyendo esto, el canal quedó enchufado.",
                      max_length=2000)
    severity: Severity = Severity.INFO
    #: Saltea dedup, quiet hours y tope. Es para la instalación: "¿llega?".
    force: bool = True


@router.get("/v1/notify")
async def notify_status(request: Request) -> dict[str, Any]:
    """Qué canales hay, con qué política, y qué pasó con los últimos mensajes."""
    return _rt(request).dispatcher.summary()


@router.post("/v1/notify/test")
async def notify_test(request: Request, body: NotifyTestRequest) -> dict[str, Any]:
    """Manda un mensaje de prueba por todos los canales. Es lo que se corre en
    lo del cliente antes de irse, para no descubrir el sábado que el SMS no
    llegaba."""
    m = Message(title=body.title, body=body.body, severity=body.severity,
                fingerprint="notify_test")
    return (await _rt(request).dispatcher.send(m, force=body.force)).as_dict()


@router.post("/v1/detectors/cost_spike/run")
async def cost_spike_run(request: Request) -> dict[str, Any]:
    """Evalúa el detector ahora mismo y, si hay pico, lo despacha con la
    política normal (dedup incluido: correrlo dos veces no manda dos SMS)."""
    return await cost_spike.run_once(_rt(request))


# --- Ingreso de alertas ----------------------------------------------------


@router.post("/v1/alerts", status_code=202)
async def alerts_receive(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """El receiver del Alertmanager del cliente. Contesta enseguida qué encoló
    y qué salteó (y por qué); el enriquecimiento y la entrega corren atrás.

    En alertmanager.yml:

        receivers:
          - name: copilot
            webhook_configs:
              - url: http://copilot:8080/v1/alerts
                http_config:
                  authorization: {credentials: <COPILOT_API_TOKEN>}
    """
    return await _rt(request).ingress.receive(payload)


@router.get("/v1/alerts")
async def alerts_recent(request: Request, limit: int = 50, only_firing: bool = False,
                        ) -> dict[str, Any]:
    """Qué llegó, qué se decidió y qué dijo el triage. Buffer en memoria."""
    rt = _rt(request)
    return {
        "pending": rt.ingress.pending,
        "total": len(rt.alert_log),
        "note": "Buffer en memoria: se pierde al reiniciar.",
        "alerts": [r.as_dict() for r in rt.alert_log.recent(limit=min(limit, 500),
                                                            only_firing=only_firing)],
    }
