"""Canal SMN de Huawei Cloud: un topic, y atrás lo que el cliente haya suscripto
—SMS, mail, HTTP, una app—.

Es el primer canal con SDK propio, y por eso importa cómo está hecho:

  - el SDK se importa adentro del constructor, no arriba. Es una dependencia
    opcional (`pip install observability-copilot[huawei]`) y un cliente sin Huawei no
    tiene por qué instalarla. Si falta, el error lo dice con el comando.
  - `max_chars` por defecto es 400 y no 4.000: un topic con suscriptores SMS
    recorta a ~490 bytes y un mensaje largo se corta a mitad de la frase que
    importaba. El que tenga sólo mail lo sube en el YAML.
  - `check()` lista las suscripciones del topic y no publica: valida
    credenciales, región y que el topic exista, sin mandarle nada a nadie. Y
    avisa si el topic no tiene suscriptores, que es un canal que parece sano y
    no le llega a nadie.

  - la región no hace falta: el URN del topic la trae adentro
    (`urn:smn:<región>:<dominio>:<nombre>`). Si igual se pasa y no coincide,
    es un error de arranque que nombra las dos, porque el síntoma sin eso es
    un "Topic not found" contra la región equivocada. Pasó en la primera
    prueba real.

Las credenciales (AK/SK) vienen del YAML por `${VAR}` y no se loguean nunca.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..ports.notify import Delivery, Message
from . import register

logger = logging.getLogger(__name__)


def _region_of(topic_urn: str) -> str:
    partes = topic_urn.split(":")
    return partes[2] if len(partes) == 5 and partes[:2] == ["urn", "smn"] else ""


@register("notify", "smn")
class SmnNotifier:
    def __init__(
        self,
        *,
        region: str = "",
        topic_urn: str = "",
        ak: str = "",
        sk: str = "",
        project_id: str = "",
        endpoint: str = "",
        max_chars: int = 400,
        name: str = "smn",
        client: Any = None,
        **_ignored: Any,
    ):
        faltan = [k for k, v in (("topic_urn", topic_urn), ("ak", ak), ("sk", sk)) if not v]
        if faltan and client is None:
            raise ValueError(
                f"notify[{name}]: faltan {', '.join(faltan)}. Van en el YAML como "
                f"${{VAR}} y los valores en el entorno, nunca en el archivo.")
        del_urn = _region_of(topic_urn)
        if region and del_urn and region != del_urn:
            raise ValueError(
                f"notify[{name}]: region es '{region}' pero el topic_urn es de "
                f"'{del_urn}'. Un topic vive en una sola región; sacá `region` "
                f"y se toma del URN.")
        region = region or del_urn
        if not region and not endpoint and client is None:
            raise ValueError(
                f"notify[{name}]: no se pudo sacar la región del topic_urn (se "
                f"esperaba urn:smn:<región>:<dominio>:<nombre>). Pasá `region`.")
        self.name = name
        self.max_chars = int(max_chars)
        self._topic = topic_urn
        # Costura para los tests, igual que `transport` en el de Prometheus.
        self._client = client if client is not None else self._build(
            region, ak, sk, project_id or None, endpoint)

    @staticmethod
    def _build(region: str, ak: str, sk: str, project_id: str | None, endpoint: str) -> Any:
        try:
            from huaweicloudsdkcore.auth.credentials import BasicCredentials
            from huaweicloudsdksmn.v2 import SmnClient
            from huaweicloudsdksmn.v2.region.smn_region import SmnRegion
        except ImportError as e:
            raise ValueError(
                "El canal smn necesita el SDK de Huawei: "
                "pip install 'observability-copilot[huawei]'") from e
        cred = BasicCredentials(ak, sk, project_id)
        b = SmnClient.new_builder().with_credentials(cred)
        if endpoint:
            b = b.with_endpoint(endpoint)
        else:
            try:
                b = b.with_region(SmnRegion.value_of(region))
            except Exception as e:
                raise ValueError(
                    f"notify: región SMN '{region}' desconocida para el SDK. Si es "
                    f"una región nueva o un endpoint privado, pasá `endpoint`.") from e
        return b.build()

    async def send(self, message: Message) -> Delivery:
        from huaweicloudsdksmn.v2 import PublishMessageRequest, PublishMessageRequestBody

        # El asunto de SMN es de hasta 512 bytes y el cuerpo lo recorta el
        # despachante a max_chars; acá sólo se arma.
        req = PublishMessageRequest(
            topic_urn=self._topic,
            body=PublishMessageRequestBody(
                subject=message.title[:200], message=message.body[: self.max_chars]),
        )
        try:
            # El SDK es síncrono; en un thread para no frenar el loop de la API.
            r = await asyncio.to_thread(self._client.publish_message, req)
            return Delivery(self.name, True, f"message_id={getattr(r, 'message_id', '')}")
        except Exception as e:
            # El mensaje del SDK trae el request id y el código de error de
            # SMN; el AK no aparece ahí, pero se recorta igual por si acaso.
            logger.warning("notify[%s]: %s: %s", self.name, type(e).__name__, str(e)[:300])
            return Delivery(self.name, False, f"{type(e).__name__}: {str(e)[:200]}")

    async def check(self) -> None:
        from huaweicloudsdksmn.v2 import ListSubscriptionsByTopicRequest

        req = ListSubscriptionsByTopicRequest(topic_urn=self._topic, limit=1)
        r = await asyncio.to_thread(self._client.list_subscriptions_by_topic, req)
        if not getattr(r, "subscription_count", 0):
            raise ValueError(
                f"el topic {self._topic} no tiene suscriptores: el canal responde "
                f"pero no le llega a nadie.")
