"""Un turno de chat: prompt, tools, presupuesto, traza.

El prompt del sistema es corto por diseño. Lo que hace que un agente de
observabilidad conteste bien no es decirle "sos un experto en SRE": es que
descubra el esquema antes de escribir una query. Un modelo que asume nombres de
métrica inventa `cpu_usage_percent` en una instalación donde la métrica se llama
`node_cpu_seconds_total`, no encuentra nada, y contesta "no hay datos" — que es
la peor respuesta posible, porque suena a diagnóstico y es un error de tipeo.

Por eso la única instrucción realmente insistente es: mirá qué hay, después
preguntá.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from .agent import audit, loop, registry
from .runtime import Runtime

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Sos un copiloto de observabilidad. Trabajás sobre el sistema de monitoreo que el
cliente ya tiene: sus métricas, sus alertas y su costo. No administrás nada — no
podés crear, modificar ni borrar recursos, y no tenés forma de hacerlo.

Cómo trabajar:

1. NUNCA adivines nombres de métricas ni de labels. Cada instalación tiene los
   suyos. Antes de escribir una query, usá `label_values` con label="__name__"
   (y `contains` para buscar por tema) para ver qué existe de verdad.
2. Escribí PromQL acotado. Agregá con `sum by (...)` o `topk(...)` en vez de
   traer miles de series. Un selector sin matchers va a ser rechazado.
3. Para "cómo viene" o "cuándo empezó" usá `promql_range`, no varias instantáneas.
4. Para "qué está disparando" usá `alerts_active`. Para explicar un disparo,
   sacá la expresión con `alert_rules` y corrésela en rango: es la query que
   la disparó, no hace falta adivinarla.
5. Podés pedir varias tools en la misma vuelta cuando son independientes: se
   ejecutan en paralelo.

Cómo responder:

- Decí el número y su unidad. "CPU al 94%" sirve; "CPU alta" no.
- Nombrá el recurso concreto (la instancia, el job, el servicio), no la categoría.
- Si un resultado vino marcado como parcial o truncado, decilo — una conclusión
  sacada de una muestra que el usuario cree completa es peor que no contestar.
- Si los datos no alcanzan para responder, decí qué te faltó y qué query lo
  respondería. No completes con lo que suele pasar en otros lados.
- Español rioplatense, directo, sin preámbulos.
"""


async def answer(
    rt: Runtime,
    question: str,
    *,
    history: list[dict[str, Any]] | None = None,
    actor: str = "anonymous",
    on_step: Any = None,
) -> loop.Result:
    """Contesta una pregunta usando las tools disponibles."""
    if rt.model is None:
        raise RuntimeError("No hay ModelPort configurado; el chat no puede funcionar.")

    ctx = rt.context()
    run_id = uuid.uuid4().hex[:12]

    mensajes: list[dict[str, Any]] = [
        {"role": "system", "content": rt.config.agent.system_prompt or SYSTEM_PROMPT},
    ]
    mensajes += list(history or [])
    mensajes.append({"role": "user", "content": question})

    async def _execute(name: str, args: dict) -> dict:
        import time

        t0 = time.monotonic()
        salida = await registry.execute(name, args, ctx)
        ms = int((time.monotonic() - t0) * 1000)
        ok = salida.get("status") == "ok"
        audit.record(
            tool=name, args=args, ok=ok, ms=ms, actor=actor, run_id=run_id,
            result_chars=len(str(salida.get("result", ""))),
            error="" if ok else str(salida.get("result", ""))[:300],
        )
        return salida

    tools = registry.definitions(ctx)
    resultado = await loop.run(
        mensajes, tools, _execute,
        model=rt.model,
        temperature=rt.config.agent.temperature,
        max_steps=rt.config.agent.max_steps,
        max_seconds=rt.config.agent.max_seconds,
        on_step=on_step,
    )
    logger.info(
        "chat: run=%s pasos=%d tokens=%d corte=%s tools=%s",
        run_id, len(resultado.steps), resultado.tokens,
        resultado.stopped_by or "-", ",".join(resultado.tools_used) or "-",
    )
    return resultado
