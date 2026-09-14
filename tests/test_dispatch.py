"""El despachante: la política que evita mandar 200 SMS una madrugada.

Todo lo de acá es lo que NO hace un canal. Si algún test se vuelve incómodo,
la respuesta no es mover la lógica al adapter de Slack.
"""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from copilot.adapters.notify_log import LogNotifier
from copilot.dispatch import Dispatcher, Policy, QuietHours
from copilot.ports.notify import Delivery, Message, Severity

T0 = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)   # 12:00 en Buenos Aires


def msg(fp: str = "a", sev: Severity = Severity.WARNING, body: str = "x") -> Message:
    return Message(title=f"t-{fp}", body=body, severity=sev, fingerprint=fp)


@pytest.fixture
def canal() -> LogNotifier:
    return LogNotifier(name="ops")


async def test_el_mismo_fingerprint_no_se_repite_antes_del_intervalo(canal):
    d = Dispatcher([canal], Policy(repeat_interval=timedelta(hours=4)))
    assert (await d.send(msg(), now=T0)).decision == "sent"
    assert (await d.send(msg(), now=T0 + timedelta(hours=1))).decision == "deduped"
    assert (await d.send(msg(), now=T0 + timedelta(hours=5))).decision == "sent"
    assert len(canal.sent) == 2


async def test_un_resolved_pasa_siempre_y_reabre_el_dedup(canal):
    d = Dispatcher([canal], Policy(repeat_interval=timedelta(hours=4)))
    await d.send(msg(), now=T0)
    assert (await d.send(msg(sev=Severity.RESOLVED), now=T0 + timedelta(minutes=5))).sent
    # Volvió a disparar antes del intervalo: es un disparo nuevo, se avisa.
    assert (await d.send(msg(), now=T0 + timedelta(minutes=10))).sent


async def test_una_resuelta_sin_disparo_avisado_no_sale(canal):
    d = Dispatcher([canal], Policy())
    assert (await d.send(msg(sev=Severity.RESOLVED), now=T0)).decision == "unpaired"
    assert canal.sent == []


async def test_repeat_after_del_mensaje_manda_sobre_el_intervalo_general(canal):
    """El detector de gasto corre cada hora y el mismo día sigue siendo 'el
    último' hasta mañana: sin esto, un pico son seis SMS."""
    d = Dispatcher([canal], Policy(repeat_interval=timedelta(hours=4)))
    pico = Message(title="pico", body="b", severity=Severity.WARNING,
                   fingerprint="cost_spike:2026-09-10", repeat_after=timedelta(days=3))
    assert (await d.send(pico, now=T0)).sent
    assert (await d.send(pico, now=T0 + timedelta(hours=5))).decision == "deduped"
    assert (await d.send(pico, now=T0 + timedelta(days=2))).decision == "deduped"
    assert (await d.send(pico, now=T0 + timedelta(days=4))).sent


async def test_quiet_hours_frena_lo_que_no_es_critico(canal):
    qh = QuietHours(start=time(22, 0), end=time(7, 0), tz="America/Argentina/Buenos_Aires")
    d = Dispatcher([canal], Policy(quiet_hours=qh))
    noche = datetime(2026, 9, 12, 3, 0, tzinfo=UTC)   # 00:00 en Buenos Aires
    assert (await d.send(msg("w"), now=noche)).decision == "quiet"
    assert (await d.send(msg("c", sev=Severity.CRITICAL), now=noche)).decision == "sent"
    assert (await d.send(msg("w2"), now=T0)).decision == "sent"


def test_quiet_hours_que_cruza_medianoche():
    qh = QuietHours(start=time(22, 0), end=time(7, 0), tz="UTC")
    assert qh.covers(datetime(2026, 1, 1, 23, 0, tzinfo=UTC))
    assert qh.covers(datetime(2026, 1, 1, 6, 59, tzinfo=UTC))
    assert not qh.covers(datetime(2026, 1, 1, 7, 0, tzinfo=UTC))
    assert not qh.covers(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))


async def test_el_tope_diario_corta_y_avisa_una_sola_vez(canal):
    """El aviso del tope es el último mensaje del día, no uno más por cada
    descartado — si no, el tope no sería un tope."""
    d = Dispatcher([canal], Policy(daily_cap=2))
    for i in range(5):
        await d.send(msg(str(i)), now=T0 + timedelta(minutes=i))
    titulos = [m.title for m in canal.sent]
    assert titulos[:2] == ["t-0", "t-1"]
    assert len(titulos) == 3 and "tope diario" in titulos[2]
    assert d.history[-1].decision == "capped"
    # Al día siguiente, el contador arranca de cero.
    assert (await d.send(msg("z"), now=T0 + timedelta(days=1))).sent


async def test_force_saltea_toda_la_politica(canal):
    d = Dispatcher([canal], Policy(daily_cap=1, repeat_interval=timedelta(days=1)))
    await d.send(msg(), now=T0)
    assert (await d.send(msg(), now=T0)).decision == "deduped"
    assert (await d.send(msg(), now=T0, force=True)).sent


async def test_recorta_al_max_chars_del_canal():
    corto = LogNotifier(name="sms", max_chars=20)
    largo = LogNotifier(name="mail", max_chars=4000)
    d = Dispatcher([corto, largo])
    await d.send(msg(body="x" * 100), now=T0)
    assert len(corto.sent[0].body) == 20 and corto.sent[0].body.endswith("…")
    assert len(largo.sent[0].body) == 100


async def test_un_canal_que_explota_no_frena_a_los_demas(canal):
    class Roto:
        name, max_chars = "roto", 100

        async def send(self, m):
            raise RuntimeError("se cayó")

        async def check(self):
            return

    d = Dispatcher([Roto(), canal])
    out = await d.send(msg(), now=T0)
    assert out.sent
    assert [(x.channel, x.ok) for x in out.deliveries] == [("roto", False), ("ops", True)]
    assert isinstance(out.deliveries[0], Delivery)


async def test_sin_canales_lo_dice():
    out = await Dispatcher([]).send(msg(), now=T0)
    assert out.decision == "no_channels"


# --- Ruteo por severidad (F2) -----------------------------------------------


def _ruteado() -> tuple[Dispatcher, LogNotifier, LogNotifier, LogNotifier]:
    guardia = LogNotifier(name="guardia")
    slack = LogNotifier(name="slack")
    log = LogNotifier(name="log")
    d = Dispatcher([guardia, slack, log], Policy(routes={
        "guardia": frozenset({Severity.CRITICAL}),
        "slack": frozenset({Severity.CRITICAL, Severity.WARNING}),
    }))
    return d, guardia, slack, log


async def test_cada_canal_recibe_solo_las_severidades_que_declara():
    d, guardia, slack, log = _ruteado()
    out = await d.send(msg("c", Severity.CRITICAL), now=T0)
    assert [x.channel for x in out.deliveries] == ["guardia", "slack", "log"]
    out = await d.send(msg("w", Severity.WARNING), now=T0)
    assert [x.channel for x in out.deliveries] == ["slack", "log"]
    out = await d.send(msg("i", Severity.INFO), now=T0)
    assert [x.channel for x in out.deliveries] == ["log"]
    assert len(guardia.sent) == 1 and len(slack.sent) == 2 and len(log.sent) == 3


async def test_lo_que_ningun_canal_acepta_queda_unrouted_y_no_deduplica():
    guardia = LogNotifier(name="guardia")
    d = Dispatcher([guardia], Policy(routes={"guardia": frozenset({Severity.CRITICAL})}))
    out = await d.send(msg("i", Severity.INFO), now=T0)
    assert out.decision == "unrouted" and out.deliveries == []
    assert guardia.sent == []
    # No quedó marcado como enviado: si mañana se rutea, sale.
    assert d.decide(msg("i", Severity.INFO), now=T0) == "unrouted"
    d.policy = Policy()
    assert d.decide(msg("i", Severity.INFO), now=T0) == "sent"


async def test_la_resuelta_va_por_donde_fue_el_disparo_no_por_su_severidad():
    """Una resuelta es severidad `resolved`, que ningún canal lista. Tiene que
    llegarle a quien vio el disparo, y sólo a ese."""
    d, guardia, _, _ = _ruteado()
    await d.send(msg("w", Severity.WARNING), now=T0)
    out = await d.send(msg("w", Severity.RESOLVED), now=T0 + timedelta(minutes=5))
    assert out.sent
    assert [x.channel for x in out.deliveries] == ["slack", "log"]
    assert guardia.sent == []


async def test_la_resuelta_no_le_llega_al_canal_que_estaba_en_el_tope():
    """Se recuerdan los canales que recibieron el disparo, no los que lo
    aceptaban: al que quedó en el tope, la resuelta le hablaría de algo que
    nunca vio."""
    a, b = LogNotifier(name="a"), LogNotifier(name="b")
    d = Dispatcher([a, b], Policy(daily_cap=2, routes={"a": frozenset({Severity.WARNING})}))
    await d.send(msg("i1", Severity.INFO), now=T0)       # sólo b
    await d.send(msg("i2", Severity.INFO), now=T0)       # b llega al tope
    out = await d.send(msg("x", Severity.WARNING), now=T0)
    assert [x.channel for x in out.deliveries] == ["a"]  # b quedó capped
    out = await d.send(msg("x", Severity.RESOLVED), now=T0)
    assert out.sent and [x.channel for x in out.deliveries] == ["a"]
    assert [m.fingerprint for m in b.sent] == ["i1", "i2", "cap:b"]


async def test_force_saltea_el_ruteo_tambien():
    d, *_ = _ruteado()
    out = await d.send(msg("t", Severity.INFO), force=True, now=T0)
    assert [x.channel for x in out.deliveries] == ["guardia", "slack", "log"]


def test_el_resumen_muestra_el_ruteo():
    d, *_ = _ruteado()
    assert d.summary()["policy"]["routes"] == {
        "guardia": ["critical"], "slack": ["critical", "warning"], "log": "all"}
