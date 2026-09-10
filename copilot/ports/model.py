"""ModelPort: el LLM, que por defecto es el del cliente.

El default no es un endpoint nuestro y eso es a propósito. Labels y valores de
métricas entran a un prompt, y "¿a dónde se van los datos?" es *la* pregunta que
hace el área de seguridad antes de firmar. Si la respuesta es "a un endpoint que
configurás vos, en tu red", la conversación se termina ahí. Si es "a un SaaS
nuestro", empieza un proceso de tres meses.

Por eso el puerto habla el dialecto OpenAI de chat completions: es el que
implementan vLLM, Ollama, TGI, LiteLLM, MaaS de Huawei, Azure OpenAI y cualquier
gateway corporativo. Un adapter cubre todo lo que un cliente pueda tener puesto.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ModelError(RuntimeError):
    def __init__(self, msg: str, *, retriable: bool = False):
        super().__init__(msg)
        self.retriable = retriable


@dataclass(frozen=True)
class ModelReply:
    """Un mensaje del asistente, completo.

    `tool_calls` va crudo tal como lo devolvió el backend: el loop tiene que
    poder reinyectarlo en el historial sin transformarlo, o el modelo pierde el
    hilo de a qué está respondiendo cada resultado.
    """

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class ModelPort(Protocol):
    name: str
    default_model: str

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> ModelReply:
        ...

    async def check(self) -> None:
        """Ping al endpoint. Levanta `ModelError` si no está usable."""
        ...
