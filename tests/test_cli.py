"""Los comandos que despachan no pisan el estado del servidor que corre al lado."""
from __future__ import annotations

import httpx
import pytest

from copilot import __main__ as cli
from copilot.adapters.notify_log import LogNotifier
from copilot.dispatch import Dispatcher
from copilot.runtime import Runtime


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "copilot.yaml"
    p.write_text(
        "server: {api_token: t, port: 18099}\n"
        "metrics: {adapter: prometheus, url: http://prom.test:9090}\n"
        "model: {adapter: openai_compat, base_url: http://m.test/v1, model: x}\n"
        "notify: [{name: ops, adapter: log}]\n"
    )
    return str(p)


def _respuesta(status: int, cuerpo: dict) -> httpx.Response:
    return httpx.Response(status, json=cuerpo, request=httpx.Request("POST", "http://x"))


def test_notify_test_lo_manda_el_servidor_si_esta_corriendo(config_path, monkeypatch, capsys):
    """Con `docker compose exec` o `kubectl exec` hay un serve al lado con el
    estado del despachante en memoria: mandar desde otro proceso pisaría ese
    estado al guardarlo. Si el servidor contesta, manda él."""
    pedidos = []

    def _post(url, **kw):
        pedidos.append((url, kw))
        return _respuesta(200, {"decision": "sent",
                                "deliveries": [{"channel": "ops", "ok": True, "detail": "log"}]})

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(cli, "build", lambda cfg: pytest.fail("no tenía que armar el runtime"))

    assert cli.main(["-c", config_path, "notify-test", "--body", "hola"]) == 0
    url, kw = pedidos[0]
    assert url == "http://127.0.0.1:18099/v1/notify/test"
    assert kw["headers"] == {"Authorization": "Bearer t"}
    assert kw["json"] == {"body": "hola", "force": True}
    assert "[OK   ] ops" in capsys.readouterr().out


def test_sin_servidor_se_manda_en_proceso(config_path, monkeypatch, capsys):
    def _post(url, **kw):
        raise httpx.ConnectError("refused", request=httpx.Request("POST", url))

    canal = LogNotifier(name="ops")
    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(cli, "build", lambda cfg: Runtime(
        config=cfg, notify=[canal], dispatcher=Dispatcher([canal])))

    assert cli.main(["-c", config_path, "notify-test"]) == 0
    assert canal.sent[-1].fingerprint == "notify_test"
    assert "[OK   ] ops" in capsys.readouterr().out


def test_detect_sin_dry_run_tambien_va_por_el_servidor(config_path, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _respuesta(
        200, {"detector": "cost_spike", "outcome": "clear", "detail": "1.0×"}))
    monkeypatch.setattr(cli, "build", lambda cfg: pytest.fail("no tenía que armar el runtime"))
    assert cli.main(["-c", config_path, "detect", "cost_spike"]) == 0
    assert '"outcome": "clear"' in capsys.readouterr().out


def test_un_servidor_que_rechaza_el_token_lo_dice_y_sigue_en_proceso(config_path, monkeypatch,
                                                                     capsys):
    canal = LogNotifier(name="ops")
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _respuesta(401, {"detail": "no"}))
    monkeypatch.setattr(cli, "build", lambda cfg: Runtime(
        config=cfg, notify=[canal], dispatcher=Dispatcher([canal])))
    assert cli.main(["-c", config_path, "notify-test"]) == 0
    assert "devolvió 401" in capsys.readouterr().err
    assert len(canal.sent) == 1
