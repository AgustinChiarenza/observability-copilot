"""El costo desde la API de facturación de Huawei Cloud (BSS).

Es el adapter de costos para cuando el dato NO está en la TSDB. El de PromQL
sigue siendo el default por las razones de `ports/cost.py`: si el gasto ya está
exportado, ir a BSS es pedir más permisos para llegar más tarde. Este existe
para el cliente que tiene la factura en BSS y nada más — que es el caso que se
describió al pedir el producto.

Tres cosas de BSS que definen cómo está escrito:

  - **va un día atrás.** Los fee records de hoy se completan mañana. Por eso
    `lag_days` es 1 por defecto, y el detector de picos no mira el día que
    todavía se está llenando.
  - **un ciclo por llamada.** `bill_date_begin/end` tienen que caer en el mismo
    `cycle` (YYYY-MM). Un rango que cruza el mes se pide en dos tandas, y el
    que llama no se entera.
  - **es global y lento.** Credencial global (no por proyecto), rate limit
    bajo, páginas de 100. Se pagina en paralelo desde un pool de threads
    porque el SDK es síncrono, y se recuerda el resultado un rato: el agente
    pregunta tres veces por lo mismo en un turno y BSS no tiene por qué
    enterarse tres veces.

El `amount` que se suma es el que se cobra (`amount`), no el de lista
(`official_amount`): un descuento comercial del 40% es lo que el cliente ve en
su factura y es contra eso que quiere comparar.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

from ..ports.cost import CostError, CostPoint, CostSlice
from . import register

logger = logging.getLogger(__name__)

_PAGE = 100
#: Sin esto los nombres de servicio vienen en chino ("弹性云服务器" por ECS)
#: aunque la cuenta sea de la nube internacional. Se vio en la primera prueba
#: real; el agente no tiene por qué traducir facturas.
_LANG = "en_US"
_PARALLEL = 6
_MAX_RECORDS = 20_000
_CACHE_TTL_S = 600.0

#: Nombres legibles para los cloud_service_type más comunes. BSS a veces manda
#: `cloud_service_type_name` y a veces no; esto es el respaldo.
_SERVICE_LABEL = {
    "hws.service.type.ec2": "ECS", "hws.service.type.ebs": "EVS",
    "hws.service.type.vpc": "VPC", "hws.service.type.obs": "OBS",
    "hws.service.type.rds": "RDS", "hws.service.type.cce": "CCE",
    "hws.service.type.elb": "ELB", "hws.service.type.dcs": "DCS",
    "hws.service.type.dds": "DDS", "hws.service.type.nat": "NAT",
    "hws.service.type.cbr": "CBR", "hws.service.type.dms": "DMS",
    "hws.service.type.cdn": "CDN", "hws.service.type.modelarts": "ModelArts",
    "hws.service.type.ces": "Cloud Eye", "hws.service.type.aom": "AOM",
}


def _months(start: date, end: date) -> list[tuple[str, date, date]]:
    """(ciclo, desde, hasta) por cada mes que toca el rango."""
    salida = []
    cur = start.replace(day=1)
    while cur <= end:
        siguiente = (cur.replace(month=cur.month + 1, day=1) if cur.month < 12
                     else date(cur.year + 1, 1, 1))
        salida.append((cur.strftime("%Y-%m"), max(start, cur),
                       min(end, siguiente - timedelta(days=1))))
        cur = siguiente
    return salida


@register("cost", "huawei_bss")
class HuaweiBssCost:
    """Costo por día, servicio y recurso desde los fee records de BSS."""

    def __init__(
        self,
        *,
        ak: str = "",
        sk: str = "",
        edition: str = "intl",
        endpoint: str = "",
        lag_days: int = 1,
        name: str = "huawei_bss",
        client: Any = None,
        **_ignored: Any,
    ):
        if client is None and (not ak or not sk):
            raise ValueError(
                f"cost[{name}]: faltan ak/sk. Van en el YAML como ${{VAR}}; la "
                f"credencial necesita sólo permisos de lectura de facturación "
                f"(bss:bill:list).")
        if edition not in ("intl", "cn"):
            raise ValueError(f"cost[{name}].edition: '{edition}' no se conoce. Usá intl o cn.")
        self.name = name
        self.lag_days = int(lag_days)
        self.currency = "USD"
        self._edition = edition
        self._client = client if client is not None else self._build(ak, sk, edition, endpoint)
        self._cache: dict[tuple[date, date], tuple[float, list[dict]]] = {}

    @staticmethod
    def _build(ak: str, sk: str, edition: str, endpoint: str) -> Any:
        try:
            from huaweicloudsdkcore.auth.credentials import GlobalCredentials
            if edition == "intl":
                from huaweicloudsdkbssintl.v2 import BssintlClient as Client
                from huaweicloudsdkbssintl.v2.region.bssintl_region import BssintlRegion as Region
                region = "ap-southeast-1"
            else:
                from huaweicloudsdkbss.v2 import BssClient as Client
                from huaweicloudsdkbss.v2.region.bss_region import BssRegion as Region
                region = "cn-north-1"
        except ImportError as e:
            raise ValueError(
                "El adapter huawei_bss necesita el SDK de Huawei: "
                "pip install 'observability-copilot[huawei]'") from e
        # BSS es un servicio global: credencial global, y la región es la del
        # endpoint de facturación, no la de los recursos.
        b = Client.new_builder().with_credentials(GlobalCredentials(ak, sk))
        b = b.with_endpoint(endpoint) if endpoint else b.with_region(Region.value_of(region))
        return b.build()

    def _request_cls(self):
        if self._edition == "intl":
            from huaweicloudsdkbssintl.v2 import ListCustomerselfResourceRecordsRequest
        else:
            from huaweicloudsdkbss.v2 import ListCustomerselfResourceRecordsRequest
        return ListCustomerselfResourceRecordsRequest

    # --- lectura ------------------------------------------------------------

    def _fetch_sync(self, start: date, end: date) -> list[dict]:
        Req = self._request_cls()
        salida: list[dict] = []
        for ciclo, desde, hasta in _months(start, end):
            def pagina(offset: int, ciclo=ciclo, desde=desde, hasta=hasta):
                # Los defaults fijan el ciclo por iteración: `pagina` corre en
                # otro thread y un closure sobre la variable del for pediría el
                # último mes N veces.
                req = Req(cycle=ciclo, bill_date_begin=desde.isoformat(),
                          bill_date_end=hasta.isoformat(), method="oneself",
                          limit=_PAGE, offset=offset, x_language=_LANG)
                return self._client.list_customerself_resource_records(req)

            try:
                primera = pagina(0)
            except Exception as e:
                raise CostError(f"{self.name}: BSS no respondió para {ciclo}: "
                                f"{type(e).__name__}: {str(e)[:200]}",
                                retriable=True) from e
            if getattr(primera, "currency", None):
                self.currency = str(primera.currency)
            salida += [self._record(r) for r in (primera.fee_records or [])]
            total = int(getattr(primera, "total_count", 0) or 0)
            offsets = list(range(_PAGE, min(total, _MAX_RECORDS), _PAGE))
            if offsets:
                # La primera página trae el total; sabiéndolo, el resto sale
                # junto. En serie, 4.500 records eran 45 llamadas una atrás de
                # otra y casi un minuto.
                with ThreadPoolExecutor(max_workers=min(_PARALLEL, len(offsets))) as ex:
                    for resp in ex.map(pagina, offsets):
                        salida += [self._record(r) for r in (resp.fee_records or [])]
            if total > _MAX_RECORDS:
                logger.warning("%s: %s tiene %d records; se leyeron %d",
                               self.name, ciclo, total, _MAX_RECORDS)
        return salida

    async def _records(self, start: date, end: date) -> list[dict]:
        llave = (start, end)
        hit = self._cache.get(llave)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
            return hit[1]
        filas = await asyncio.to_thread(self._fetch_sync, start, end)
        # Se podan las vencidas al guardar: el detector pide una ventana nueva
        # por día y cada entrada son hasta 20.000 records por mes — sin esto,
        # en un mes el pod tiene la facturación entera del cliente en RAM.
        ahora = time.monotonic()
        self._cache = {k: v for k, v in self._cache.items() if ahora - v[0] < _CACHE_TTL_S}
        self._cache[llave] = (ahora, filas)
        return filas

    @staticmethod
    def _record(r: Any) -> dict:
        svc = str(getattr(r, "cloud_service_type", "") or "")
        return {
            "day": str(getattr(r, "bill_date", "") or ""),
            "service": (getattr(r, "cloud_service_type_name", None)
                        or _SERVICE_LABEL.get(svc) or svc or "unknown"),
            "service_type": svc,
            "resource_id": str(getattr(r, "resource_id", "") or ""),
            "resource_name": str(getattr(r, "resource_name", "") or ""),
            "region": str(getattr(r, "region", "") or ""),
            "amount": float(getattr(r, "amount", 0) or 0),
        }

    async def daily_series(self, *, start: date, end: date) -> list[CostPoint]:
        por_dia: dict[str, float] = defaultdict(float)
        for f in await self._records(start, end):
            if f["day"]:
                por_dia[f["day"][:10]] += f["amount"]
        return [CostPoint(day=date.fromisoformat(d), amount=round(m, 4), currency=self.currency)
                for d, m in sorted(por_dia.items())]

    async def by_service(self, *, start: date, end: date, limit: int = 10) -> list[CostSlice]:
        acumulado: dict[str, float] = defaultdict(float)
        for f in await self._records(start, end):
            acumulado[f["service"]] += f["amount"]
        filas = sorted(acumulado.items(), key=lambda kv: kv[1], reverse=True)
        return [CostSlice(key=k, amount=round(v, 4), currency=self.currency)
                for k, v in filas[:limit]]

    async def by_resource(
        self, *, start: date, end: date, limit: int = 20, service: str | None = None,
    ) -> list[CostSlice]:
        acumulado: dict[str, float] = defaultdict(float)
        meta: dict[str, dict[str, str]] = {}
        aguja = (service or "").lower()
        for f in await self._records(start, end):
            if aguja and aguja not in f["service"].lower() and aguja not in f["service_type"]:
                continue
            rid = f["resource_id"] or f["resource_name"] or "(sin id)"
            acumulado[rid] += f["amount"]
            meta.setdefault(rid, {k: v for k, v in (
                ("name", f["resource_name"]), ("service", f["service"]),
                ("region", f["region"])) if v})
        filas = sorted(acumulado.items(), key=lambda kv: kv[1], reverse=True)
        return [CostSlice(key=k, amount=round(v, 4), currency=self.currency, labels=meta[k])
                for k, v in filas[:limit]]

    async def check(self) -> None:
        # Un día, la página más chica: valida credencial y permiso sin leer la
        # factura entera.
        Req = self._request_cls()
        ayer = date.today() - timedelta(days=1)  # noqa: DTZ011 — BSS es de día calendario
        req = Req(cycle=ayer.strftime("%Y-%m"), bill_date_begin=ayer.isoformat(),
                  bill_date_end=ayer.isoformat(), method="oneself", limit=1, offset=0,
                  x_language=_LANG)
        try:
            await asyncio.to_thread(self._client.list_customerself_resource_records, req)
        except Exception as e:
            raise CostError(f"{self.name}: {type(e).__name__}: {str(e)[:200]}") from e
