"""El armado: registro de adapters y cableado de puertos.

El criterio con el que se mide si el diseño de puertos está bien: agregar un
backend tiene que ser escribir un archivo en `adapters/` y nada más. Si para
sumar uno hubiera que editar un `if` en el core, está mal.
"""
from __future__ import annotations

import pytest

from copilot import adapters
from copilot.config import Config
from copilot.runtime import build


def test_los_adapters_de_fabrica_estan_registrados():
    catalogo = adapters.catalog()
    assert "prometheus" in catalogo["metrics"]
    assert "promql" in catalogo["cost"]
    assert "openai_compat" in catalogo["model"]
    assert {"webhook", "log"} <= set(catalogo["notify"])


def test_un_adapter_inexistente_lista_los_que_si_existen():
    """El error tiene que servir para arreglar el YAML sin ir a leer el código."""
    with pytest.raises(adapters.UnknownAdapter) as e:
        adapters.build("metrics", "influxdb", {})
    assert "prometheus" in str(e.value)


def test_registrar_dos_veces_el_mismo_nombre_es_un_error_de_programa():
    with pytest.raises(RuntimeError, match="dos veces"):
        adapters.register("metrics", "prometheus")(object)


def _cfg(**extra) -> Config:
    return Config.from_dict({
        "server": {"api_token": "t"},
        "metrics": {"adapter": "prometheus", "url": "http://prom:9090"},
        "model": {"adapter": "openai_compat", "base_url": "http://m/v1", "model": "x"},
        **extra,
    })


def test_el_costo_por_promql_recibe_el_puerto_de_metricas():
    """Es la dependencia que hace posible que la instalación más común no pida
    ninguna credencial de facturación."""
    rt = build(_cfg(cost={"adapter": "promql", "daily": "sum(costo)"}))
    assert rt.cost is not None
    assert rt.cost._m is rt.metrics


def test_sin_canales_configurados_queda_el_de_log():
    """Un error de tipeo en el YAML de notify produciría un sistema que parece
    sano, no falla y no avisa nada. Con el canal de log ese silencio se puede
    encontrar con un grep."""
    rt = build(_cfg())
    assert [c.name for c in rt.notify] == ["log"]


def test_el_presupuesto_del_yaml_llega_al_adapter():
    rt = build(_cfg(budget={"max_series": 7, "max_range": "3d"}))
    assert rt.metrics.budget.max_series == 7
    assert rt.metrics.budget.max_range.days == 3


def test_describe_dice_que_quedo_enchufado():
    rt = build(_cfg())
    d = rt.describe()
    assert d["metrics"] == "prometheus" and d["model"] == "openai_compat"
    assert d["cost"] is None


async def test_el_chequeo_no_corta_al_primer_fallo():
    """Quien corre `preflight` quiere la lista completa de lo que está mal, no
    descubrirlos de a uno con un reinicio en el medio."""
    rt = build(_cfg())
    estado = await rt.check()
    assert set(estado) >= {"metrics", "cost", "model", "notify:log"}
    assert estado["cost"] == "not configured"
    # metrics y model apuntan a hosts que no existen: los dos tienen que
    # aparecer con su motivo, no sólo el primero.
    assert estado["metrics"].startswith("MetricsError")
    assert estado["model"].startswith("ModelError")


def test_una_opcion_invalida_falla_al_armar_y_no_en_la_primera_consulta():
    with pytest.raises(ValueError, match=r"cost\.daily"):
        build(_cfg(cost={"adapter": "promql"}))
