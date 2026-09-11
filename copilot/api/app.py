"""La app HTTP: el borde del producto.

Sin front, a propósito. Las superficies son esta API, y en F6 los bots de Slack
y Teams — que hablan con ella. Un cliente que quiera pantalla la arma en su
Grafana, que es donde su gente ya mira.

Tres rutas de salud, y son tres cosas distintas:

  /healthz   el proceso está vivo. No toca nada afuera. Es la liveness probe: si
             respondiera por dependencias, Kubernetes reiniciaría el pod cada
             vez que el Prometheus del cliente se pone lento, que es exactamente
             cuando no querés perder el agente.
  /readyz    las dependencias responden. Readiness: sacame del balanceador si no
             puedo trabajar.
  /v1/status qué quedó enchufado. Para humanos, en una instalación nueva.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import scheduler, telemetry
from ..config import Config, ConfigError, load
from ..runtime import Runtime, build
from .routes import router
from .security import auth_middleware

logger = logging.getLogger(__name__)


def create_app(config: Config | None = None) -> FastAPI:
    """Arma la app. `config=None` la carga del disco (el camino del contenedor)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = config or load()
        app.state.config = cfg
        app.state.runtime = build(cfg)
        # El chequeo de arranque no bloquea: si el Prometheus del cliente está
        # reiniciándose justo ahora, el pod tiene que levantar igual y quedar
        # listo cuando el otro vuelva. Queda en el log y en /readyz.
        try:
            estado = await app.state.runtime.check()
            for puerto, r in estado.items():
                telemetry.PORT_CHECKS.labels(port=puerto).set(1 if r == "ok" else 0)
            malos = {k: v for k, v in estado.items() if v not in ("ok", "not configured")}
            if malos:
                logger.warning("preflight: puertos con problemas: %s", malos)
            else:
                logger.info("preflight: todos los puertos responden")
        except Exception as e:
            logger.warning("preflight: no se pudo chequear (%s)", e)
        tareas = scheduler.start(app.state.runtime)
        try:
            yield
        finally:
            await scheduler.stop(tareas)

    app = FastAPI(
        title="Copilot",
        version="0.1.0",
        description="Addon de observabilidad: lee las métricas, alertas y costos "
                    "que el cliente ya tiene.",
        lifespan=lifespan,
        docs_url="/docs",
    )

    app.middleware("http")(auth_middleware)
    app.middleware("http")(_metrics_middleware)
    app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            telemetry.render().decode("utf-8"),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.exception_handler(ConfigError)
    async def _config_error(_r: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    return app


async def _metrics_middleware(request: Request, call_next):
    """Cuenta y cronometra.

    La `path` que se usa como label es la ruta declarada, no la URL: con la URL
    cruda, cada id distinto crea una serie nueva y en una semana el copiloto le
    explota la cardinalidad al Prometheus del cliente. Sería un final irónico.
    """
    t0 = time.monotonic()
    respuesta = await call_next(request)
    ruta = request.scope.get("route")
    etiqueta = getattr(ruta, "path", None) or "unmatched"
    telemetry.HTTP_REQUESTS.labels(
        method=request.method, path=etiqueta, status=str(respuesta.status_code)).inc()
    telemetry.HTTP_DURATION.labels(
        method=request.method, path=etiqueta).observe(time.monotonic() - t0)
    return respuesta


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime
