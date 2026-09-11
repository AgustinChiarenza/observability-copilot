"""El canal SMN, con un cliente falso: se prueba qué se le pide al SDK, no el SDK."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from copilot.adapters.notify_smn import SmnNotifier
from copilot.ports.notify import Message, Severity


class FakeSmn:
    def __init__(self, subs: int = 1, fail: bool = False):
        self.subs, self.fail = subs, fail
        self.published: list = []

    def publish_message(self, req):
        if self.fail:
            raise RuntimeError("SMN.0001 request_id=abc")
        self.published.append(req)
        return SimpleNamespace(message_id="m-1")

    def list_subscriptions_by_topic(self, req):
        return SimpleNamespace(subscription_count=self.subs)


def test_sin_credenciales_dice_que_falta_y_como_ponerlo():
    with pytest.raises(ValueError, match="ak, sk"):
        SmnNotifier(region="la-south-2", topic_urn="urn:x")


async def test_publica_asunto_y_cuerpo_recortado():
    fake = FakeSmn()
    ch = SmnNotifier(topic_urn="urn:x", max_chars=30, client=fake)
    d = await ch.send(Message(title="Pico", body="x" * 100, severity=Severity.WARNING))
    assert d.ok and "m-1" in d.detail
    req = fake.published[0]
    assert req.topic_urn == "urn:x"
    assert req.body.subject == "Pico"
    assert len(req.body.message) == 30


async def test_un_error_del_sdk_es_una_entrega_fallida_no_una_excepcion():
    ch = SmnNotifier(topic_urn="urn:x", client=FakeSmn(fail=True))
    d = await ch.send(Message(title="t", body="b"))
    assert not d.ok and "SMN.0001" in d.detail


async def test_check_no_publica_y_avisa_si_nadie_escucha():
    fake = FakeSmn(subs=0)
    with pytest.raises(ValueError, match="no tiene suscriptores"):
        await SmnNotifier(topic_urn="urn:x", client=fake).check()
    assert fake.published == []
    await SmnNotifier(topic_urn="urn:x", client=FakeSmn(subs=2)).check()
