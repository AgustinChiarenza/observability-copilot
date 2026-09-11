"""Cloud Eye como MetricsPort: el selector, la expansión por recurso, el
período según presupuesto y el mapeo de alarmas. Cliente falso, modelos
reales del SDK: lo que se prueba es qué se le pide a CES y cómo se lee lo que
devuelve."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from huaweicloudsdkces.v1.model import (
    BatchListMetricDataResponse,
    BatchMetricData,
    Datapoint,
    ListMetricsResponse,
    MetaData,
    MetricInfoList,
    MetricsDimension,
)
from huaweicloudsdkces.v2.model import (
    AlarmHistoryItemV2,
    AlarmHistoryItemV2Condition,
    AlarmHistoryItemV2Metric,
    ListAlarmHistoriesResponse,
    ListAlarmRespBodyAlarms,
    ListAlarmRulesResponse,
    PolicyResp,
)

from copilot.adapters.metrics_cloudeye import CloudEyeMetrics, parse_query
from copilot.agent.registry import Context, definitions
from copilot.ports.metrics import Budget, MetricsError


class FakeV1:
    def __init__(self, metrics: list[tuple[str, str, dict]], points: int = 3):
        self.metrics = metrics
        self.points = points
        self.batches: list = []

    def list_metrics(self, req):
        return ListMetricsResponse(
            metrics=[MetricInfoList(namespace=ns, metric_name=m, unit="%",
                                    dimensions=[MetricsDimension(name=k, value=v)
                                                for k, v in d.items()])
                     for ns, m, d in self.metrics],
            meta_data=MetaData(count=len(self.metrics), total=len(self.metrics), marker=""),
        )

    def batch_list_metric_data(self, req):
        self.batches.append(req.body)
        t0 = 1_700_000_000_000
        return BatchListMetricDataResponse(metrics=[
            BatchMetricData(
                namespace=m.namespace, metric_name=m.metric_name, unit="%",
                dimensions=m.dimensions,
                datapoints=[Datapoint(average=10.0 * (i + 1), max=99.0, timestamp=t0 + i * 60_000)
                            for i in range(self.points)],
            ) for m in req.body.metrics
        ])


class FakeV2:
    def list_alarm_histories(self, req):
        return ListAlarmHistoriesResponse(alarm_histories=[AlarmHistoryItemV2(
            alarm_id="al1", name="CPU alta", status="alarm", level=2,
            first_alarm_time=datetime(2026, 9, 11, 10, 0, tzinfo=UTC),
            metric=AlarmHistoryItemV2Metric(namespace="SYS.ECS", metric_name="cpu_util",
                                            dimensions=[]),
            condition=AlarmHistoryItemV2Condition(period=300, filter="average",
                                                  comparison_operator=">", value=80,
                                                  unit="%", count=3),
        )])

    def list_alarm_rules(self, req):
        return ListAlarmRulesResponse(alarms=[
            ListAlarmRespBodyAlarms(alarm_id="al1", name="CPU alta", namespace="SYS.ECS",
                                    enabled=True, policies=[PolicyResp(
                                        metric_name="cpu_util", period=300, filter="average",
                                        comparison_operator=">", value=80, unit="%", count=3,
                                        level=2)]),
            ListAlarmRespBodyAlarms(alarm_id="al2", name="Apagada", namespace="SYS.RDS",
                                    enabled=False, policies=[]),
        ])


@pytest.fixture
def v1():
    return FakeV1([
        ("SYS.ECS", "cpu_util", {"instance_id": "i-1"}),
        ("SYS.ECS", "cpu_util", {"instance_id": "i-2"}),
        ("SYS.RDS", "rds001_cpu_util", {"rds_instance_id": "r-1"}),
    ])


@pytest.fixture
def port(v1):
    return CloudEyeMetrics(client_v1=v1, client_v2=FakeV2(), budget=Budget(max_points=100))


# --- El selector -----------------------------------------------------------


def test_el_selector_se_parsea_con_y_sin_dimensiones():
    s = parse_query('max(SYS.ECS/cpu_util{instance_id="abc", x="y"})')
    assert (s.namespace, s.metric, s.agg) == ("SYS.ECS", "cpu_util", "max")
    assert s.dims == {"instance_id": "abc", "x": "y"}
    assert parse_query("SYS.RDS/rds001_cpu_util").dims == {}


@pytest.mark.parametrize("malo,pista", [
    ("up", "SYS.ECS/cpu_util"),
    ("rate(SYS.ECS/cpu_util[5m])", "No hay funciones PromQL"),
    ("SYS.ECS/cpu_util{a=b}", "comillas dobles"),
])
def test_promql_o_selectores_rotos_se_rechazan_con_una_pista(malo, pista):
    with pytest.raises(MetricsError, match=pista):
        parse_query(malo)


# --- Lectura ------------------------------------------------------------------


async def test_sin_dimensiones_se_expande_a_todos_los_recursos(port, v1):
    r = await port.instant("SYS.ECS/cpu_util")
    assert len(r.series) == 2
    assert {s.labels["instance_id"] for s in r.series} == {"i-1", "i-2"}
    assert r.series[0].labels["__name__"] == "SYS.ECS/cpu_util"
    # Instant se queda con el último punto y a período crudo.
    assert r.series[0].last.value == 30.0
    assert v1.batches[-1].period == "1"


async def test_con_dimension_se_pide_solo_ese_recurso(port, v1):
    await port.instant('SYS.ECS/cpu_util{instance_id="i-1"}')
    assert [d.value for d in v1.batches[-1].metrics[0].dimensions] == ["i-1"]
    assert len(v1.batches[-1].metrics) == 1


async def test_la_agregacion_elige_el_campo_del_datapoint(port, v1):
    r = await port.instant("max(SYS.ECS/cpu_util)")
    assert v1.batches[-1].filter == "max"
    assert r.series[0].last.value == 99.0


async def test_el_periodo_respeta_max_points(port, v1):
    fin = datetime.now(UTC)
    r = await port.range("SYS.ECS/cpu_util", start=fin - timedelta(days=7), end=fin)
    assert r.step_s == 14400.0          # 7d / 100 puntos → 6048s → el siguiente de la escalera
    assert v1.batches[-1].period == "14400"


async def test_mas_recursos_que_max_series_queda_marcado(v1):
    port = CloudEyeMetrics(client_v1=v1, client_v2=FakeV2(), budget=Budget(max_series=1))
    r = await port.instant("SYS.ECS/cpu_util")
    assert len(r.series) == 1 and r.truncated


async def test_una_metrica_que_nadie_reporta_lo_dice(port):
    with pytest.raises(MetricsError, match="ningún recurso"):
        await port.instant("SYS.ECS/inventada")


async def test_label_values_descubre_nombres_namespaces_y_dimensiones(port):
    assert await port.label_values("__name__") == ["SYS.ECS/cpu_util", "SYS.RDS/rds001_cpu_util"]
    assert await port.label_values("namespace") == ["SYS.ECS", "SYS.RDS"]
    assert await port.label_values("instance_id", matches=["SYS.ECS/cpu_util"]) == ["i-1", "i-2"]


# --- Alertas ------------------------------------------------------------------


async def test_las_alarmas_activas_traen_metrica_severidad_y_condicion(port):
    a = (await port.alerts())[0]
    assert a.name == "CPU alta" and a.state == "firing"
    assert a.labels["metric"] == "SYS.ECS/cpu_util" and a.severity == "major"
    assert a.annotations["summary"] == "average(SYS.ECS/cpu_util) > 80%"
    assert a.active_at.year == 2026


async def test_las_reglas_traen_la_condicion_y_marcan_las_apagadas(port):
    reglas = {r.name: r for r in await port.rules()}
    assert reglas["CPU alta"].expression == "average(SYS.ECS/cpu_util) > 80% durante 3×300s"
    assert reglas["CPU alta"].duration_s == 900
    assert reglas["Apagada"].health == "err" and "deshabilitada" in reglas["Apagada"].last_error


# --- Integración con el core --------------------------------------------------


def test_targets_health_no_aparece_en_el_catalogo(port):
    nombres = [d["function"]["name"] for d in definitions(Context(metrics=port))]
    assert "targets_health" not in nombres and "promql_instant" in nombres


async def test_el_prompt_del_sistema_lleva_la_sintaxis(config, port, model):
    from copilot import service
    from copilot.runtime import Runtime

    await service.answer(Runtime(config=config, metrics=port, model=model), "hola")
    assert "SYS.ECS/cpu_util" in model.seen[0][0]["content"]
    assert "no es PromQL" in model.seen[0][0]["content"]


def test_sin_credenciales_dice_que_permisos_hacen_falta():
    with pytest.raises(ValueError, match="ces:metricData:list"):
        CloudEyeMetrics(region="la-south-2")


async def test_metadata_es_todo_gauge_con_la_unidad_de_ces(port):
    fichas = await port.metadata(contains="cpu")
    assert [(m.name, m.type, m.unit) for m in fichas] == [
        ("SYS.ECS/cpu_util", "gauge", "%"), ("SYS.RDS/rds001_cpu_util", "gauge", "%")]
    assert len(await port.metadata(limit=1)) == 1
