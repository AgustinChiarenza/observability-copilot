"""BSS como CostPort: agrupar fee records por día, servicio y recurso, y
partir un rango por ciclo, que es lo que la API exige."""
from __future__ import annotations

from datetime import date

import pytest
from huaweicloudsdkbssintl.v2.model import ListCustomerselfResourceRecordsResponse, ResFeeRecordV2

from copilot.adapters.cost_huawei_bss import HuaweiBssCost, _months
from copilot.ports.cost import CostError


def rec(day: str, svc: str, rid: str, amount: float, name: str = "") -> ResFeeRecordV2:
    return ResFeeRecordV2(bill_date=day, cloud_service_type=svc, resource_id=rid,
                          resource_name=name, amount=amount, official_amount=amount * 2)


class FakeBss:
    def __init__(self, records: list[ResFeeRecordV2], page: int = 100, fail: bool = False):
        self.records, self.page, self.fail = records, page, fail
        self.calls: list = []

    def list_customerself_resource_records(self, req):
        if self.fail:
            raise RuntimeError("BSS.0401 unauthorized")
        self.calls.append(req)
        filas = [r for r in self.records if req.bill_date_begin <= r.bill_date <= req.bill_date_end]
        lote = filas[req.offset:req.offset + req.limit]
        return ListCustomerselfResourceRecordsResponse(
            fee_records=lote, total_count=len(filas), currency="USD")


REGISTROS = [
    rec("2026-09-01", "hws.service.type.ec2", "i-1", 10, "web"),
    rec("2026-09-01", "hws.service.type.rds", "r-1", 30),
    rec("2026-09-02", "hws.service.type.ec2", "i-1", 12, "web"),
    rec("2026-09-02", "hws.service.type.ec2", "i-2", 5, "batch"),
]


@pytest.fixture
def port():
    return HuaweiBssCost(client=FakeBss(REGISTROS))


def test_un_rango_que_cruza_el_mes_se_parte_por_ciclo():
    assert _months(date(2026, 8, 30), date(2026, 9, 2)) == [
        ("2026-08", date(2026, 8, 30), date(2026, 8, 31)),
        ("2026-09", date(2026, 9, 1), date(2026, 9, 2)),
    ]


async def test_la_serie_diaria_suma_lo_que_se_cobra_no_lo_de_lista(port):
    s = await port.daily_series(start=date(2026, 9, 1), end=date(2026, 9, 2))
    assert [(p.day.isoformat(), p.amount) for p in s] == [("2026-09-01", 40.0), ("2026-09-02", 17.0)]
    assert s[0].currency == "USD"


async def test_por_servicio_con_nombre_legible(port):
    filas = await port.by_service(start=date(2026, 9, 1), end=date(2026, 9, 2))
    assert [(f.key, f.amount) for f in filas] == [("RDS", 30.0), ("ECS", 27.0)]


async def test_por_recurso_acotado_a_un_servicio(port):
    filas = await port.by_resource(start=date(2026, 9, 1), end=date(2026, 9, 2), service="ecs")
    assert [(f.key, f.amount) for f in filas] == [("i-1", 22.0), ("i-2", 5.0)]
    assert filas[0].labels == {"name": "web", "service": "ECS"}


async def test_pagina_hasta_leer_todo():
    fake = FakeBss(REGISTROS, page=1)
    port = HuaweiBssCost(client=fake)
    import copilot.adapters.cost_huawei_bss as mod
    mod_page, mod._PAGE = mod._PAGE, 1
    try:
        s = await port.daily_series(start=date(2026, 9, 1), end=date(2026, 9, 2))
    finally:
        mod._PAGE = mod_page
    assert sum(p.amount for p in s) == 57.0
    assert len(fake.calls) == 4


async def test_dentro_del_ttl_no_vuelve_a_bss(port):
    fake = port._client
    await port.daily_series(start=date(2026, 9, 1), end=date(2026, 9, 2))
    await port.by_service(start=date(2026, 9, 1), end=date(2026, 9, 2))
    assert len(fake.calls) == 1


async def test_un_error_de_bss_es_un_costerror_reintentable():
    port = HuaweiBssCost(client=FakeBss([], fail=True))
    with pytest.raises(CostError, match=r"BSS\.0401") as e:
        await port.daily_series(start=date(2026, 9, 1), end=date(2026, 9, 1))
    assert e.value.retriable


def test_va_un_dia_atras_por_defecto_y_exige_credenciales():
    assert HuaweiBssCost(client=FakeBss([])).lag_days == 1
    with pytest.raises(ValueError, match="bss:bill:list"):
        HuaweiBssCost()
