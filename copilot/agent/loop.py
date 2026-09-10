"""Loop de ejecución de tools con presupuesto.

El modelo pide una tool, el código la ejecuta y le devuelve el resultado, y así
hasta que responde sin pedir nada más. Es lo que permite encadenar —ver qué
targets hay, después consultar la métrica de los caídos, después cruzar con el
gasto— que con un ruteo por regex es imposible: elige una y listo.

Tres cosas que el loop garantiza, y que son la diferencia entre "el modelo llama
tools" y "podés dejarlo suelto contra el productivo de un cliente":

  presupuesto  tope de pasos y de tiempo. Un modelo que se enrosca pidiendo la
               misma query no puede quemar la sesión ni saturarle el TSDB.
  validación   lo que el modelo pide se valida antes de ejecutarse. El nombre lo
               eligió él y puede estar inventado.
  traza        cada paso queda registrado: qué pidió, con qué argumentos, si
               salió bien y cuánto tardó. Es lo que se le muestra a una auditoría
               cuando pregunta qué hizo exactamente el agente contra sus datos.

Portado del proyecto anterior. Lo que se sacó: los guards de URLs inventadas del
cotizador, que eran específicos de aquel dominio. Lo que se conserva textual: la
ejecución en paralelo de las tools de una misma vuelta —el modelo las pide
juntas porque son independientes, y en serie el turno tarda la suma en vez del
máximo— y el aviso por `on_step` apenas termina cada una, para que una corrida
larga no se vea como "0 pasos" hasta el final, que es justo cuando ya no sirve
mirarla.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..ports.model import ModelPort

logger = logging.getLogger(__name__)

#: Recorte del resultado de una tool antes de mandárselo al modelo. Un
#: `query_range` mal acotado puede volver con megabytes; sin techo, una sola
#: tool le come el contexto a todo el turno.
MAX_TOOL_CHARS = 8_000


@dataclass
class Step:
    tool: str
    args: dict
    ok: bool
    result: str
    ms: int


@dataclass
class Result:
    text: str = ""
    steps: list[Step] = field(default_factory=list)
    tokens: int = 0
    model: str = ""
    #: "" | "budget" | "time" — por qué se cortó, si se cortó.
    stopped_by: str = ""

    @property
    def tools_used(self) -> list[str]:
        return [s.tool for s in self.steps]


def _args_of(tool_call: dict) -> dict:
    """Los argumentos vienen como string JSON; el modelo a veces manda basura."""
    raw = (tool_call.get("function") or {}).get("arguments") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        args = json.loads(raw)
        return args if isinstance(args, dict) else {}
    except json.JSONDecodeError:
        logger.warning("agent.loop: argumentos no parseables: %s", str(raw)[:200])
        return {}


def _as_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)


async def run(
    messages: list[dict],
    tools: list[dict],
    execute: Callable[[str, dict], Awaitable[dict]],
    *,
    model: ModelPort,
    model_name: str | None = None,
    temperature: float = 0.2,
    max_steps: int = 12,
    max_seconds: float = 180.0,
    on_step: Callable[[Step], None] | None = None,
) -> Result:
    """Corre el ciclo pedir → ejecutar → devolver hasta que el modelo termine.

    `execute(name, args)` es lo que realmente llama a la tool. Es el punto donde
    se valida: el loop no confía en el nombre que eligió el modelo.
    """
    history = list(messages)
    res = Result(model=model_name or model.default_model)
    t0 = time.monotonic()

    for step_n in range(max_steps):
        if time.monotonic() - t0 > max_seconds:
            res.stopped_by = "time"
            logger.warning("agent.loop: cortado por tiempo tras %d pasos", step_n)
            break

        reply = await model.complete(
            history, tools=tools or None, model=model_name, temperature=temperature)
        res.tokens += reply.tokens
        if reply.model:
            res.model = reply.model

        if not reply.wants_tools:
            res.text = reply.content
            return res

        # El mensaje del asistente con los tool_calls tiene que quedar en el
        # historial: sin él, los `role: tool` que siguen no tienen a qué
        # responder y el modelo se pierde.
        history.append({
            "role": "assistant",
            "content": reply.content or "",
            "tool_calls": reply.tool_calls,
        })

        async def _one(tc: dict) -> tuple[dict, Step, str]:
            name = (tc.get("function") or {}).get("name") or ""
            args = _args_of(tc)
            t = time.monotonic()
            try:
                out = await execute(name, args)
            except Exception as e:
                out = {"status": "error", "result": f"{type(e).__name__}: {e}"}
            ms = int((time.monotonic() - t) * 1000)
            text = _as_text(out.get("result", ""))[:MAX_TOOL_CHARS]
            step = Step(name, args, out.get("status") == "ok", text[:500], ms)
            # El aviso sale acá, apenas ESTA tool termina. Si esperáramos al
            # gather, dos tools en paralelo (2s y 30s) avisarían las dos a los
            # 30s: progreso que llega cuando ya no sirve.
            if on_step is not None:
                try:
                    on_step(step)
                except Exception as e:
                    logger.warning("agent.loop: on_step falló: %s", e)
            return tc, step, text

        calls = reply.tool_calls
        outputs = (
            [await _one(calls[0])] if len(calls) == 1
            else list(await asyncio.gather(*(_one(tc) for tc in calls)))
        )

        # El orden de los resultados sigue al de los tool_calls, no al de
        # finalización: el modelo espera una respuesta por cada uno, en su orden.
        for tc, step, text in outputs:
            res.steps.append(step)
            logger.info("agent.loop: paso %d tool=%s ok=%s %dms",
                        step_n + 1, step.tool, step.ok, step.ms)
            history.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or step.tool,
                "name": step.tool,
                "content": text or "(sin resultado)",
            })
    else:
        res.stopped_by = "budget"
        logger.warning("agent.loop: agotó los %d pasos", max_steps)

    # Se acabó el presupuesto con tools pendientes: se pide el cierre sin tools,
    # así el usuario recibe lo que se llegó a averiguar en vez de un turno vacío.
    if not res.text:
        history.append({
            "role": "user",
            "content": ("Respondé ahora con lo que averiguaste, sin pedir más "
                        "herramientas. Si te faltó algo, decí qué te faltó."),
        })
        reply = await model.complete(history, model=model_name, temperature=temperature)
        res.tokens += reply.tokens
        res.text = reply.content

    # Un turno en blanco es el peor resultado posible: el usuario no sabe si
    # falló, si no había datos o si se colgó. Pasa cuando el modelo insiste con
    # tool_calls aun sin tools ofrecidas. Si llegamos acá sin texto, se dice qué
    # pasó y se muestra lo que sí se averiguó.
    if not res.text:
        hechas = ", ".join(dict.fromkeys(s.tool for s in res.steps if s.ok))
        motivo = {"budget": f"agoté los {max_steps} pasos disponibles",
                  "time": f"me pasé de los {max_seconds:.0f}s disponibles"}.get(
                      res.stopped_by, "no llegué a una respuesta")
        res.text = (
            f"No pude cerrar una respuesta: {motivo}."
            + (f" Alcancé a consultar: {hechas}." if hechas else "")
            + " Probá acotando la pregunta a un recurso o a una ventana más corta."
        )
        logger.warning("agent.loop: turno sin texto (corte=%s, %d pasos)",
                       res.stopped_by or "-", len(res.steps))
    return res
