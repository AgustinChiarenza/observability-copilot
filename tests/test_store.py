"""Lo que sobrevive a un reinicio: auditoría, alertas recibidas y el estado
del despachante. Y lo que pasa cuando el disco no colabora."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from copilot.adapters.notify_log import LogNotifier
from copilot.agent import audit
from copilot.alerts.ingress import AlertLog, Record
from copilot.dispatch import Dispatcher, Policy
from copilot.ports.alerts import Signal, SignalStatus
from copilot.ports.notify import Message, Severity
from copilot.runtime import build
from copilot.store import Jsonl, State, Storage
from tests.test_runtime import _cfg

pytestmark = pytest.mark.anyio


# --- Los archivos -------------------------------------------------------------


def test_jsonl_devuelve_las_ultimas_n_en_orden(tmp_path):
    j = Jsonl(tmp_path / "a.jsonl")
    for i in range(10):
        j.append({"i": i})
    assert [f["i"] for f in j.tail(3)] == [7, 8, 9]


def test_jsonl_saltea_una_linea_cortada_por_un_apagado(tmp_path):
    p = tmp_path / "a.jsonl"
    Jsonl(p).append({"i": 1})
    with p.open("a") as f:
        f.write('{"i": 2, "trunc')
    assert Jsonl(p).tail(10) == [{"i": 1}]


def test_jsonl_rota_por_tamano_y_sigue_leyendo_la_generacion_anterior(tmp_path):
    j = Jsonl(tmp_path / "a.jsonl", max_bytes=40)
    for i in range(20):
        j.append({"i": i})
    assert (tmp_path / "a.jsonl.1").exists()
    ultimos = [f["i"] for f in j.tail(5)]
    assert ultimos == [15, 16, 17, 18, 19]


def test_state_arranca_vacio_si_el_archivo_esta_roto(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{no es json")
    assert State(p).load() == {}
    State(p).save({"a": 1})
    assert State(p).load() == {"a": 1}
    assert not p.with_suffix(".tmp").exists()


def test_un_disco_que_no_escribe_no_frena_nada(tmp_path, caplog):
    """Un volumen de solo lectura o lleno se loguea; el turno sigue."""
    j = Jsonl(tmp_path / "a.jsonl")
    j.path.write_text("")
    j.path.chmod(0o444)
    try:
        j.append({"i": 1})   # no levanta
    finally:
        j.path.chmod(0o644)
    assert "no se pudo escribir" in caplog.text


def test_sin_path_no_hay_archivos():
    s = Storage(None)
    assert not s.durable and s.jsonl("x") is None and s.state("x") is None


# --- Auditoría ------------------------------------------------------------------


def test_la_auditoria_se_recarga_al_arrancar(tmp_path):
    audit.attach(Jsonl(tmp_path / "audit.jsonl"))
    audit.record(tool="promql_instant", args={"query": "up"}, ok=True, ms=3, actor="ana")
    assert audit.summary()["durable"] is True

    audit.reset()
    assert audit.summary()["entries"] == 0
    assert audit.attach(Jsonl(tmp_path / "audit.jsonl")) == 1
    fila = audit.timeline()[0]
    assert fila["actor"] == "ana" and fila["args"] == {"query": "up"}


# --- Alertas recibidas ---------------------------------------------------------


def _señal(fp: str, status=SignalStatus.FIRING) -> Signal:
    return Signal(fingerprint=fp, name="TargetCaido", status=status,
                  starts_at=datetime(2026, 9, 11, 10, tzinfo=UTC),
                  labels={"severity": "critical", "job": "x"},
                  annotations={"summary": "se cayó", "runbook_url": "http://rb"})


def test_el_log_de_alertas_guarda_al_terminar_y_recarga_entero(tmp_path):
    log = AlertLog(store=Jsonl(tmp_path / "alerts.jsonl"))
    r = Record(signal=_señal("f1"), received_at=datetime.now(UTC).isoformat())
    log.add(r)
    r.decision, r.enrichment = "sent", {"expression": "up == 0"}
    r.deliveries = [{"channel": "ops", "ok": True, "detail": ""}]
    log.persist(r)

    otro = AlertLog(store=Jsonl(tmp_path / "alerts.jsonl"))
    assert len(otro) == 1 and otro.durable
    rec = otro.recent()[0]
    assert rec.signal.fingerprint == "f1" and rec.signal.severity == "critical"
    assert rec.signal.annotations["runbook_url"] == "http://rb"
    assert rec.decision == "sent" and rec.enrichment == {"expression": "up == 0"}
    assert rec.deliveries[0]["channel"] == "ops"
    assert rec.as_dict() == r.as_dict()


# --- Despachante -----------------------------------------------------------------


def _msg(fp: str) -> Message:
    return Message(title="t", body="b", severity=Severity.WARNING, fingerprint=fp)


async def test_el_dedup_y_el_tope_sobreviven_al_reinicio(tmp_path):
    """El caso que importa: se reinicia a las 3 AM y el proceso nuevo NO
    re-manda lo que el viejo ya frenó."""
    estado = State(tmp_path / "dispatch.json")
    canal = LogNotifier(name="ops")
    politica = Policy(repeat_interval=timedelta(hours=4), daily_cap=2)
    d = Dispatcher([canal], politica, state=estado)
    assert (await d.send(_msg("f1"))).decision == "sent"
    assert (await d.send(_msg("f2"))).decision == "sent"

    canal2 = LogNotifier(name="ops")
    d2 = Dispatcher([canal2], politica, state=State(tmp_path / "dispatch.json"))
    assert (await d2.send(_msg("f1"))).decision == "deduped"
    assert (await d2.send(_msg("f3"))).decision == "capped"
    assert d2.summary()["sent_today"] == {"ops": 2}
    assert d2.summary()["durable"] is True
    # El aviso de tope salió una sola vez, en el proceso nuevo.
    assert [m.fingerprint for m in canal2.sent] == ["cap:ops"]
    assert (await d2.send(_msg("f4"))).decision == "capped"
    assert len(canal2.sent) == 1


async def test_el_estado_viejo_se_poda_al_guardar(tmp_path):
    estado = State(tmp_path / "dispatch.json")
    canal = LogNotifier(name="ops")
    d = Dispatcher([canal], Policy(repeat_interval=timedelta(hours=1)), state=estado)
    ayer = datetime.now(UTC) - timedelta(days=1)
    await d.send(_msg("vieja"), now=ayer)
    await d.send(_msg("nueva"))
    guardado = estado.load()
    assert list(guardado["last_sent"]) == ["nueva"]
    assert all(f == datetime.now(UTC).date().isoformat() for _, f, _ in guardado["sent_today"])


def test_un_estado_ilegible_no_tira_el_arranque(tmp_path):
    p = tmp_path / "dispatch.json"
    p.write_text('{"last_sent": {"f": "no es fecha"}}')
    d = Dispatcher([LogNotifier(name="ops")], state=State(p))
    assert d.decide(_msg("f")) == "sent"


# --- El runtime ------------------------------------------------------------------


def test_build_engancha_todo_al_mismo_directorio(tmp_path):
    rt = build(_cfg(storage={"path": str(tmp_path / "datos")}))
    assert rt.storage.durable and rt.alert_log.durable
    assert audit.summary()["durable"] is True
    assert rt.dispatcher.summary()["durable"] is True
    assert (tmp_path / "datos").is_dir()


def test_build_sin_storage_queda_en_memoria_y_lo_dice(caplog):
    rt = build(_cfg())
    assert not rt.storage.durable and not rt.alert_log.durable
    assert audit.summary()["durable"] is False
    assert "storage.path no configurado" in caplog.text
