"""El arnés del agente: presupuesto, default deny y traza.

Es lo que se le promete a un cliente antes de dejarlo correr contra sus datos.
Cada test de acá corresponde a una frase de esa promesa.
"""
from __future__ import annotations

import asyncio

import pytest

from copilot.agent import audit, loop, permissions, registry
from tests.conftest import FakeModel


async def _noop(_name, _args):
    return {"status": "ok", "result": "listo"}


# --- Presupuesto ------------------------------------------------------------


async def test_corta_al_agotar_los_pasos_y_igual_contesta(model):
    """Un modelo enroscado no puede quemar la sesión — pero el usuario tiene que
    recibir lo que se llegó a averiguar, no un turno vacío."""
    model.script = [FakeModel.call("promql_instant", {"query": "up"}) for _ in range(3)]
    model.script.append(FakeModel.say("con lo que vi, los targets están arriba"))

    r = await loop.run([{"role": "user", "content": "?"}], [], _noop,
                       model=model, max_steps=3)
    assert r.stopped_by == "budget"
    assert len(r.steps) == 3
    assert "targets" in r.text


async def test_si_ni_el_cierre_da_texto_se_explica_en_vez_de_quedar_en_blanco(model):
    """Un turno vacío es el peor resultado: el usuario no sabe si falló, si no
    había datos o si se colgó. Pasa cuando el modelo insiste con tool_calls aun
    sin tools ofrecidas."""
    model.script = [FakeModel.call("promql_instant", {"query": "up"}) for _ in range(9)]
    r = await loop.run([{"role": "user", "content": "?"}], [], _noop,
                       model=model, max_steps=3)
    assert r.stopped_by == "budget"
    assert "agoté los 3 pasos" in r.text
    assert "promql_instant" in r.text


async def test_corta_por_tiempo(model):
    async def lenta(_n, _a):
        await asyncio.sleep(0.05)
        return {"status": "ok", "result": "ok"}

    model.script = [FakeModel.call("x") for _ in range(50)]
    model.script.append(FakeModel.say("cierre"))
    r = await loop.run([{"role": "user", "content": "?"}], [], lenta,
                       model=model, max_steps=50, max_seconds=0.1)
    assert r.stopped_by == "time"


async def test_las_tools_de_una_misma_vuelta_corren_en_paralelo(model):
    """El modelo las pide juntas porque son independientes. En serie el turno
    tarda la suma; en paralelo, lo que tarde la más lenta."""
    async def lenta(_n, _a):
        await asyncio.sleep(0.1)
        return {"status": "ok", "result": "ok"}

    model.script = [
        loop_reply := FakeModel.call("a"),
        FakeModel.say("listo"),
    ]
    loop_reply.tool_calls.append({
        "id": "c2", "type": "function",
        "function": {"name": "b", "arguments": "{}"}})

    import time
    t0 = time.monotonic()
    r = await loop.run([{"role": "user", "content": "?"}], [], lenta, model=model)
    transcurrido = time.monotonic() - t0

    assert len(r.steps) == 2
    assert transcurrido < 0.19, "las dos tools tardaron como si fueran en serie"


# --- Default deny -----------------------------------------------------------


async def test_una_tool_inventada_por_el_modelo_no_se_ejecuta():
    """El nombre lo elige el modelo y a veces lo inventa. Que no la vea en el
    catálogo no alcanza: puede pedirla igual."""
    r = await registry.execute("borrar_todo", {}, registry.Context())
    assert r["status"] == "error"
    assert "no está clasificada" in r["result"]


def test_toda_tool_registrada_esta_clasificada_como_lectura():
    """Si alguna vez aparece una que no lo esté, este test es el lugar donde hay
    que pararse a pensar, no el lugar donde hay que agregarle una excepción."""
    clasificacion = permissions.classified()
    assert clasificacion, "no se registró ninguna tool"
    assert set(clasificacion.values()) == {"read"}


def test_no_se_puede_registrar_la_misma_tool_dos_veces():
    with pytest.raises(RuntimeError, match="dos veces"):
        registry.tool("promql_instant", "duplicada")(_dummy)


async def _dummy(ctx):  # pragma: no cover — sólo para el test de arriba
    return None


async def test_una_tool_que_explota_devuelve_el_error_como_resultado(model):
    """Un fallo de tool es información para el modelo —puede corregir la query—
    y no motivo para cortarle el turno al usuario."""
    async def explota(_n, _a):
        raise RuntimeError("el backend dijo que no")

    model.script = [FakeModel.call("promql_instant"), FakeModel.say("no pude")]
    r = await loop.run([{"role": "user", "content": "?"}], [], explota, model=model)
    assert r.steps[0].ok is False
    assert r.text == "no pude"


async def test_argumentos_invalidos_vuelven_con_la_firma():
    """Le alcanza al modelo para corregirse solo en el paso siguiente."""
    r = await registry.execute("promql_instant", {"inventado": 1}, registry.Context())
    assert r["status"] == "error"
    assert "argumentos inválidos" in r["result"]


# --- Historial --------------------------------------------------------------


async def test_el_resultado_de_la_tool_llega_al_modelo(model):
    """Sin el mensaje del asistente con los tool_calls, los `role: tool` que
    siguen no tienen a qué responder y el modelo se pierde."""
    async def ejecuta(_n, _a):
        return {"status": "ok", "result": {"up": 7}}

    model.script = [
        FakeModel.call("targets_health"),
        lambda hist: FakeModel.say(f"vi esto: {hist[-1]['content']}"),
    ]
    r = await loop.run([{"role": "user", "content": "?"}], [], ejecuta, model=model)
    assert '"up": 7' in r.text

    ultimo = model.seen[-1]
    assert ultimo[-2]["role"] == "assistant" and ultimo[-2]["tool_calls"]
    assert ultimo[-1]["role"] == "tool"


async def test_avisa_cada_paso_apenas_termina(model):
    """Si se avisara al final, dos tools en paralelo (2s y 30s) avisarían las dos
    a los 30s: progreso que llega cuando ya no sirve."""
    vistos = []
    model.script = [FakeModel.call("promql_instant"), FakeModel.say("ok")]
    await loop.run([{"role": "user", "content": "?"}], [], _noop,
                   model=model, on_step=vistos.append)
    assert [s.tool for s in vistos] == ["promql_instant"]


async def test_un_observador_roto_no_corta_el_turno(model):
    def rompe(_paso):
        raise ValueError("el que escucha explotó")

    model.script = [FakeModel.call("promql_instant"), FakeModel.say("igual contesté")]
    r = await loop.run([{"role": "user", "content": "?"}], [], _noop,
                       model=model, on_step=rompe)
    assert r.text == "igual contesté"


async def test_argumentos_no_parseables_no_tumban_el_paso(model):
    roto = FakeModel.call("promql_instant")
    roto.tool_calls[0]["function"]["arguments"] = "{esto no es json"
    model.script = [roto, FakeModel.say("ok")]
    r = await loop.run([{"role": "user", "content": "?"}], [], _noop, model=model)
    assert r.steps[0].args == {}


# --- Auditoría --------------------------------------------------------------


def test_la_auditoria_guarda_la_query_pero_no_los_datos():
    """Guardar el resultado sería hacer una copia de las métricas del cliente en
    un buffer que nadie pidió. Queda el tamaño, que es lo que hace falta."""
    audit.record(tool="promql_instant", args={"query": "up"}, ok=True, ms=12,
                 result_chars=4096, actor="ana")
    fila = audit.timeline()[0]
    assert fila["args"] == {"query": "up"}
    assert fila["result_chars"] == 4096
    assert "result" not in fila


def test_la_auditoria_dice_que_no_es_durable():
    """Que sea un buffer en memoria tiene que estar dicho donde se lee, o alguien
    lo va a confundir con un registro de compliance."""
    assert audit.summary()["durable"] is False
