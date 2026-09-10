"""Canal webhook genérico: un POST con JSON.

Cubre Slack (incoming webhook), Teams, DingTalk, Mattermost, un Lambda del
cliente o su propia cola. Los canales con formato propio —SMN, Slack con
bloques, mail— llegan en F3; este es el que hace que el producto sea usable
antes de que existan, porque casi todo equipo ya tiene un webhook al que
apuntar.

`template: slack` existe porque el incoming webhook de Slack exige `{"text":...}`
y rechaza cualquier otra cosa con un 400 que no explica nada.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..ports.notify import Delivery, Message
from . import register

logger = logging.getLogger(__name__)


@register("notify", "webhook")
class WebhookNotifier:
    def __init__(
        self,
        *,
        url: str = "",
        template: str = "json",
        headers: dict[str, str] | None = None,
        timeout_s: float = 15.0,
        verify_tls: bool = True,
        max_chars: int = 3500,
        name: str = "webhook",
        **_ignored: Any,
    ):
        if not url:
            raise ValueError(f"notify[{name}].url: falta la URL del webhook.")
        if template not in ("json", "slack", "text"):
            raise ValueError(
                f"notify[{name}].template: '{template}' no se conoce. "
                f"Usá json, slack o text.")
        self.name = name
        self.max_chars = int(max_chars)
        self._url = url
        self._template = template
        self._headers = dict(headers or {})
        self._timeout = timeout_s
        self._verify = verify_tls

    def _payload(self, m: Message) -> Any:
        cuerpo = m.body[: self.max_chars]
        if self._template == "slack":
            texto = f"*{m.title}*\n{cuerpo}"
            return {"text": texto[: self.max_chars]}
        if self._template == "text":
            return f"{m.title}\n{cuerpo}"
        return {
            "severity": str(m.severity),
            "title": m.title,
            "body": cuerpo,
            "fingerprint": m.fingerprint,
            "url": m.url,
            "labels": m.labels,
        }

    async def send(self, message: Message) -> Delivery:
        carga = self._payload(message)
        kwargs: dict[str, Any] = (
            {"content": carga} if isinstance(carga, str) else {"json": carga})
        try:
            async with httpx.AsyncClient(timeout=self._timeout, verify=self._verify) as c:
                r = await c.post(self._url, headers=self._headers, **kwargs)
            if r.status_code >= 400:
                return Delivery(self.name, False, f"{r.status_code} — {r.text[:200]}")
            return Delivery(self.name, True, str(r.status_code))
        except httpx.HTTPError as e:
            # Un canal caído no puede tumbar al que lo llamó: el despachante
            # sigue con los demás y esto queda como entrega fallida.
            logger.warning("notify[%s]: %s", self.name, e)
            return Delivery(self.name, False, f"{type(e).__name__}: {e}")

    async def check(self) -> None:
        # Un webhook no tiene forma de validarse sin publicar algo, y publicar
        # en el canal de guardia del cliente cada vez que arranca un pod es
        # exactamente cómo se pierde la confianza en las alertas. Se valida la
        # forma de la URL y nada más.
        if not self._url.startswith(("http://", "https://")):
            raise ValueError(f"notify[{self.name}].url: tiene que empezar con http:// o https://")
