"""La config tiene que fallar temprano, con el nombre del campo, y toda junta."""
from __future__ import annotations

import pytest

from copilot.config import Config, ConfigError, expand, parse_duration
from copilot.ports.notify import Severity


def test_los_secretos_salen_del_entorno(monkeypatch):
    monkeypatch.setenv("MI_TOKEN", "s3cr3t")
    assert expand({"auth": {"token": "${MI_TOKEN}"}}) == {"auth": {"token": "s3cr3t"}}


def test_una_variable_sin_valor_ni_default_corta_el_arranque():
    """El modo de fallar importa: sin esto, ${VAR} sin definir se convertía en
    string vacío y el síntoma aparecía tres horas después como un 401 raro."""
    with pytest.raises(ConfigError) as e:
        expand({"metrics": {"url": "${NO_EXISTE_JAMAS}"}})
    assert "NO_EXISTE_JAMAS" in str(e.value)
    assert "metrics.url" in str(e.value)


def test_una_variable_sin_valor_pero_con_default_usa_el_default(monkeypatch):
    monkeypatch.delenv("TAMPOCO_EXISTE", raising=False)
    assert expand("${TAMPOCO_EXISTE:-http://localhost:9090}") == "http://localhost:9090"


def test_una_variable_vacia_cuenta_como_ausente(monkeypatch):
    """`FOO=` en un .env es lo mismo que no ponerla: si no, el default nunca
    aplica y el usuario ve un error apuntando a una variable que "sí definió"."""
    monkeypatch.setenv("VACIA", "")
    assert expand("${VACIA:-fallback}") == "fallback"


@pytest.mark.parametrize(("raw", "segundos"), [
    ("30s", 30), ("5m", 300), ("2h", 7200), ("31d", 2678400), ("1w", 604800), (45, 45),
])
def test_las_duraciones_se_escriben_con_unidad(raw, segundos):
    assert parse_duration(raw, where="x").total_seconds() == segundos


def test_una_duracion_sin_unidad_reconocible_nombra_el_campo():
    with pytest.raises(ConfigError) as e:
        parse_duration("un rato", where="budget.max_range")
    assert "budget.max_range" in str(e.value)


def test_los_problemas_salen_todos_juntos():
    """De a uno obligaría a un reinicio entre cada par. Quien está configurando
    esto quiere arreglar los cuatro y volver a levantar."""
    cfg = Config.from_dict({"agent": {"max_steps": 0}})
    problemas = "\n".join(cfg.problems())
    assert "metrics:" in problemas      # falta el puerto obligatorio
    assert "model:" in problemas
    assert "agent.max_steps" in problemas
    assert "server.api_token" in problemas


def test_un_canal_repetido_es_un_error_de_arranque():
    """El nombre es la llave del ruteo por severidad: repetido, uno gana en
    silencio y el otro no entrega nunca."""
    cfg = Config.from_dict({
        "server": {"api_token": "t"},
        "metrics": {"adapter": "prometheus", "url": "http://x"},
        "model": {"adapter": "openai_compat", "base_url": "http://y", "model": "m"},
        "notify": [{"name": "ops", "adapter": "log"}, {"name": "ops", "adapter": "log"}],
    })
    assert any("repetido" in p for p in cfg.problems())


def test_sin_token_no_arranca_salvo_que_lo_digas_explicito():
    base = {
        "metrics": {"adapter": "prometheus", "url": "http://x"},
        "model": {"adapter": "openai_compat", "base_url": "http://y", "model": "m"},
    }
    assert any("api_token" in p for p in Config.from_dict(base).problems())
    abierto = Config.from_dict({**base, "server": {"allow_insecure": True}})
    assert abierto.problems() == []


def test_un_adapter_sin_nombre_lo_dice():
    with pytest.raises(ConfigError) as e:
        Config.from_dict({"metrics": {"url": "http://x"}})
    assert "metrics.adapter" in str(e.value)


# --- Ruteo por severidad (F2) -----------------------------------------------

_BASE = {
    "server": {"api_token": "t"},
    "metrics": {"adapter": "prometheus", "url": "http://x"},
    "model": {"adapter": "openai_compat", "base_url": "http://y", "model": "m"},
}


def test_severities_sale_del_canal_y_entra_a_la_politica():
    cfg = Config.from_dict({**_BASE, "notify": [
        {"name": "guardia", "adapter": "log", "severities": ["critical"]},
        {"name": "chat", "adapter": "log", "severities": "warning"},
        {"name": "todo", "adapter": "log"},
    ]})
    assert cfg.dispatch.routes == {
        "guardia": frozenset({Severity.CRITICAL}), "chat": frozenset({Severity.WARNING})}
    # El adapter no la ve: no es una opción suya.
    assert all("severities" not in c.options for c in cfg.notify)
    assert cfg.problems() == []


@pytest.mark.parametrize("malo, pista", [
    (["urgente"], "no es una severidad"),
    (["resolved"], "no se lista"),
    ([], "lista no vacía"),
])
def test_severities_invalidas_nombran_el_campo(malo, pista):
    with pytest.raises(ConfigError, match=r"notify\[0\]\.severities") as e:
        Config.from_dict({**_BASE, "notify": [
            {"name": "x", "adapter": "log", "severities": malo}]})
    assert pista in str(e.value)


def test_una_severidad_que_nadie_recibe_es_un_problema_de_arranque():
    cfg = Config.from_dict({**_BASE, "notify": [
        {"name": "guardia", "adapter": "log", "severities": ["critical"]}]})
    problemas = cfg.problems()
    assert len(problemas) == 1 and "warning, info" in problemas[0]
