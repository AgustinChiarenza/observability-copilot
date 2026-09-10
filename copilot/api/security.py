"""Auth del borde: un token compartido.

Un token, no usuarios: esto se instala dentro de la red del cliente y lo llaman
su Alertmanager, su bot de Slack y su gente. Montar registro, sesiones y roles
sería inventar un directorio de identidades al lado del que ya tienen. Cuando
haga falta más (F6, con el bot y usuarios reales), lo que corresponde es hablar
con su OIDC, no crecer esto.

Las rutas de salud quedan afuera del token a propósito: si Kubernetes tuviera
que autenticarse para preguntar si el pod está vivo, el día que el token esté
mal el síntoma sería un CrashLoopBackOff sin explicación.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

OPEN_PATHS = frozenset({"/healthz", "/readyz", "/metrics", "/docs", "/openapi.json"})


async def auth_middleware(request: Request, call_next):
    cfg = getattr(request.app.state, "config", None)
    token = getattr(getattr(cfg, "server", None), "api_token", "") or ""

    if not token or request.url.path in OPEN_PATHS:
        return await call_next(request)

    entregado = ""
    cabecera = request.headers.get("authorization", "")
    if cabecera.lower().startswith("bearer "):
        entregado = cabecera[7:].strip()

    # compare_digest y no `==`: comparar tokens con el operador de igualdad
    # filtra por dónde difieren a través del tiempo que tarda en cortar.
    if not entregado or not hmac.compare_digest(entregado, token):
        logger.warning("auth: token inválido en %s desde %s",
                       request.url.path,
                       request.client.host if request.client else "?")
        return JSONResponse(
            status_code=401,
            content={"detail": "Token inválido o ausente. Mandá "
                               "'Authorization: Bearer <server.api_token>'."},
        )
    return await call_next(request)
