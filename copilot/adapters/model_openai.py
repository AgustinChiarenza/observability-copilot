"""Adapter para cualquier endpoint compatible con chat completions de OpenAI.

Uno solo cubre vLLM, Ollama, TGI, LiteLLM, MaaS de Huawei, Azure OpenAI y
prácticamente cualquier gateway corporativo. Eso es lo que permite que el
default sea el endpoint del cliente y no uno nuestro, que es la diferencia entre
una instalación y un proceso de aprobación de tres meses.

Dos detalles que se aprendieron a los golpes en el proyecto anterior y por eso
están acá:

  - los modelos con razonamiento devuelven `content: null` y ponen el texto en
    `reasoning_content`. Sin el fallback, el turno sale vacío contra glm y
    parientes.
  - `reasoning_content` **no** se stremea al usuario aunque esté: es el
    monólogo interno del modelo, y filtrarlo al chat es filtrar borradores que
    a veces contradicen la respuesta final.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..ports.model import ModelError, ModelReply
from . import register

logger = logging.getLogger(__name__)


@register("model", "openai_compat")
class OpenAICompatModel:
    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout_s: float = 120.0,
        require_https: bool = False,
        verify_tls: bool = True,
        headers: dict[str, str] | None = None,
        name: str = "openai_compat",
        **_ignored: Any,
    ):
        if not base_url:
            raise ValueError(
                "model.base_url: falta. Es la base del endpoint compatible con "
                "OpenAI, por ejemplo http://vllm:8000/v1")
        if not model:
            raise ValueError("model.model: falta el nombre del modelo a usar.")
        # El prompt lleva labels y valores de las métricas del cliente. En claro
        # sobre la red de él es una discusión que no querés tener con su equipo
        # de seguridad — y el flag existe para que sea su decisión explícita.
        if require_https and not base_url.startswith("https://"):
            raise ValueError(
                f"model.base_url tiene que ser https con require_https activado. "
                f"Vino: {base_url}")
        self.name = name
        self.default_model = model
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout_s
        self._verify = verify_tls
        self._headers = dict(headers or {})
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    async def _post(self, payload: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, verify=self._verify) as c:
                r = await c.post(f"{self.base_url}/chat/completions",
                                 headers={**self._headers, "Content-Type": "application/json"},
                                 json=payload)
        except httpx.TimeoutException as e:
            raise ModelError(f"{self.name}: timeout a los {self._timeout:.0f}s.",
                             retriable=True) from e
        except httpx.HTTPError as e:
            raise ModelError(f"{self.name}: no se pudo alcanzar {self.base_url} ({e}).",
                             retriable=True) from e

        if r.status_code == 401 or r.status_code == 403:
            raise ModelError(f"{self.name}: credencial rechazada ({r.status_code}). "
                             f"Revisá model.api_key.", retriable=False)
        if r.status_code == 429:
            raise ModelError(f"{self.name}: rate limit del endpoint.", retriable=True)
        if r.status_code >= 500:
            raise ModelError(f"{self.name}: el endpoint devolvió {r.status_code}.",
                             retriable=True)
        if r.status_code >= 400:
            raise ModelError(f"{self.name}: {r.status_code} — {r.text[:300]}",
                             retriable=False)
        return r.json()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> ModelReply:
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        data = await self._post(payload)
        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"{self.name}: respuesta sin choices — {str(data)[:300]}",
                             retriable=False)
        msg = choices[0].get("message") or {}
        return ModelReply(
            content=str(msg.get("content") or msg.get("reasoning_content") or ""),
            tool_calls=list(msg.get("tool_calls") or []),
            tokens=int((data.get("usage") or {}).get("total_tokens") or 0),
            model=str(data.get("model") or payload["model"]),
            raw=msg,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        """Deltas de texto. Sólo `content`: `reasoning_content` se descarta."""
        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        async with (
            httpx.AsyncClient(timeout=self._timeout, verify=self._verify) as c,
            c.stream(
                "POST", f"{self.base_url}/chat/completions",
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
            ) as r,
        ):
                if r.status_code >= 400:
                    cuerpo = (await r.aread()).decode("utf-8", "replace")[:300]
                    raise ModelError(f"{self.name}: {r.status_code} — {cuerpo}",
                                     retriable=r.status_code >= 500)
                async for linea in r.aiter_lines():
                    if not linea or not linea.startswith("data:"):
                        continue
                    cuerpo = linea[5:].strip()
                    if cuerpo == "[DONE]":
                        break
                    try:
                        chunk = json.loads(cuerpo)
                    except json.JSONDecodeError:
                        continue
                    opciones = chunk.get("choices") or []
                    if not opciones:
                        continue
                    token = (opciones[0].get("delta") or {}).get("content") or ""
                    if token:
                        yield token

    async def check(self) -> None:
        await self.complete([{"role": "user", "content": "ping"}], temperature=0.0)
