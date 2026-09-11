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
