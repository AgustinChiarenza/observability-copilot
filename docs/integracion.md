# Integrar el copiloto en un stack de observabilidad propio

Para el equipo que ya tiene Prometheus, Alertmanager y su propio tablero, y
quiere un agente que trabaje sobre esos datos sin reemplazar nada. Este
documento es el contrato de la API: qué se le manda, qué contesta y qué puede
salir mal. La instalación (imagen, Secret, chart, NetworkPolicy) está en
[`instalacion.md`](instalacion.md).

Todo es HTTP + JSON. La especificación OpenAPI viva está en `/docs` (Swagger)
y `/openapi.json` del propio servicio; si algo de acá difiere de lo que dice
`/docs`, gana `/docs`.

## 1. Autenticación

Un único token, `server.api_token` (en el chart, `COPILOT_API_TOKEN` del
Secret). Se manda como Bearer en cada request:

```
Authorization: Bearer <token>
```

Sin token o con token equivocado: **401** con `{"detail": "..."}`.

Quedan abiertos sin token, para probes y scraping: `/healthz`, `/readyz`,
`/metrics`, `/docs`, `/openapi.json`.

Si van a tener más de un consumidor (el bot, el Alertmanager, un script),
conviene igual un solo token por instalación y distinguirlos por el campo
`actor` del chat (§3): la auditoría lo guarda.

## 2. Salud

| Ruta | Para qué | Respuesta |
|---|---|---|
| `GET /healthz` | liveness: el proceso vive. No toca nada afuera. | `200 ok` |
| `GET /readyz` | readiness: puede contestar. Chequea los puertos. | `200` o `503` con `{"ready": bool, "ports": {...}}` |
| `GET /v1/status` | qué quedó enchufado y con qué límites | JSON, ver abajo |
| `GET /metrics` | Prometheus de la propia instancia | text/plain |

`/readyz` devuelve 200 sólo si **métricas y modelo** contestan. Costo y
canales de aviso pueden estar rotos y el servicio sigue listo: se puede
preguntar sobre métricas igual. `ports` trae un string por puerto: `"ok"` o
el error resumido.

`/v1/status`:

```json
{
  "version": "0.1.0",
  "ports": {"metrics": "prometheus", "cost": "huawei_bss", "model": "openai_compat",
            "model_name": "glm-5.2", "notify": ["log", "guardia"]},
  "tools": ["alert_rules", "alerts_active", "alerts_received", "cost_by_resource", ...],
  "permissions": {"alert_rules": "read", ...},
  "budget": {"max_steps": 12, "max_seconds": 180, "max_series": 200,
             "max_points": 11000, "max_range_days": 31},
  "detectors": {"cost_spike": {"enabled": true, "interval_s": 3600, "threshold": 1.5, "window_days": 7}},
  "storage": {"durable": true, "path": "/data"}
}
```

`budget` son los topes con los que el agente consulta **su** Prometheus: nunca
va a traer más de `max_series` series ni más de `max_points` puntos por
query, ni rangos mayores a `max_range_days`. Si una pregunta necesita más, la
tool falla y el modelo lo dice en la respuesta en lugar de tirar abajo el
Prometheus.

## 3. Chat: `POST /v1/chat`

Una pregunta en lenguaje natural sobre sus datos, contestada con tools de
**sólo lectura** (§5). El modelo no puede escribir, silenciar ni borrar nada:
no existe tool para eso.

### Request

```json
{
  "message": "¿qué está firing ahora y desde cuándo?",
  "history": [
    {"role": "user", "content": "¿cuánto gastamos ayer?"},
    {"role": "assistant", "content": "Ayer se gastaron USD 412, 97% en MaaS..."}
  ],
  "actor": "slack:U0123ABC"
}
```

| Campo | Tipo | Límite | Notas |
|---|---|---|---|
| `message` | string | 1..8000 chars | obligatorio |
| `history` | lista de `{role, content}` | ≤ 40 turnos, `content` ≤ 8000 | opcional. `role` sólo `user` o `assistant`; un `system` es **422**. |
| `actor` | string | ≤ 120 | opcional, default `"anonymous"`. Va a la auditoría; usen algo que identifique a la persona o al bot. |

El servicio **no guarda conversaciones**: es stateless por turno. Si el bot
quiere contexto ("¿y la semana pasada?"), manda el historial en `history`.
Mandar sólo los últimos N turnos alcanza; no hace falta toda la sesión.

### Response (200)

```json
{
  "text": "Hay 2 alertas firing: `KubePodCrashLooping` en `payments` desde las 03:12 UTC ...",
  "steps": [
    {"tool": "alerts_active", "args": {}, "ok": true, "ms": 84},
    {"tool": "promql_range", "args": {"query": "...", "lookback": "1h"}, "ok": true, "ms": 213}
  ],
  "tokens": 3120,
  "model": "glm-5.2",
  "stopped_by": ""
}
```

- `text`: la respuesta, en el idioma de la pregunta, en Markdown liviano
  (listas, backticks). Si la pregunta no se puede contestar con los datos
  disponibles, lo dice ahí; no inventa números.
- `steps`: la traza. **Muéstrenla** (o al menos ténganla a un click): un
  agente que da un número sin decir qué query corrió no es auditable. `args`
  son los argumentos exactos con los que se llamó la tool; `ok: false` es una
  tool que falló (el modelo lo vio y siguió o lo explicó).
- `tokens`: consumo del turno, para que lo contabilicen si el modelo lo cobra
  por token.
- `stopped_by`: vacío si el modelo terminó por sí solo. `"budget"` si llegó a
  `agent.max_steps` tools, `"time"` si pasó `agent.max_seconds`. En ambos
  casos `text` trae lo mejor que tenía hasta ahí; conviene mostrarlo con una
  marca de "respuesta parcial".

### Errores

| Código | Cuándo | Qué hacer |
|---|---|---|
| 401 | sin Bearer o token incorrecto | revisar el Secret |
| 422 | body inválido (mensaje vacío, `role: system`, >40 turnos, >8000 chars) | `detail` dice qué campo |
| 502 | el modelo o una tool tiró una excepción no manejada | `detail` trae `TipoDeError: mensaje`. Reintentar una vez; si persiste, mirar el log del pod |
| 503 | sin modelo configurado o runtime no armado | `/readyz` lo confirma |

### Tiempos

Un turno tarda lo que tarde el modelo por sus tools: entre 3 y 40 segundos
es lo normal; el tope duro es `agent.max_seconds` (default 180 s), después de
eso vuelve con `stopped_by: "time"`. **El cliente HTTP tiene que tener un
timeout mayor a ese** (recomendado: `max_seconds + 15`). Para un bot de chat,
mandar un "estoy mirando..." al usuario y contestar cuando llegue; no
bloquear el hilo.

No hay streaming en esta versión: la respuesta llega entera.

### Ejemplo mínimo

```bash
curl -sS -X POST "$COPILOT_URL/v1/chat" \
  -H "Authorization: Bearer $COPILOT_API_TOKEN" -H 'Content-Type: application/json' \
  --max-time 200 \
  -d '{"message":"¿qué targets están down?","actor":"cli:agustin"}'
```

```python
import httpx

r = httpx.post(f"{URL}/v1/chat", headers={"Authorization": f"Bearer {TOKEN}"},
               json={"message": pregunta, "history": ultimos_turnos, "actor": usuario},
               timeout=200)
r.raise_for_status()
respuesta = r.json()
print(respuesta["text"])
for paso in respuesta["steps"]:
    print(f"  {paso['tool']}({paso['args']}) {'ok' if paso['ok'] else 'FALLÓ'} {paso['ms']}ms")
```

## 4. Alertas: el receiver de Alertmanager

### `POST /v1/alerts`

Recibe el webhook estándar de Alertmanager (versión `4` del payload; lo que
manda cualquier Alertmanager ≥ 0.16). Por cada alerta del grupo decide en el
momento si la va a procesar y contesta **202 enseguida**; el enriquecimiento
(tendencia de la expresión, alertas activas, costo si aplica, triage con el
modelo) y la entrega por los canales corren después, en background.

Configuración del lado de ellos, sin tocar el ruteo que ya tienen:

```yaml
route:
  routes:
    - receiver: copilot
      continue: true
      matchers: [severity =~ "warning|critical"]
receivers:
  - name: copilot
    webhook_configs:
      - url: http://copilot-observability-copilot.copilot.svc:8080/v1/alerts
        send_resolved: true
        http_config:
          authorization:
            credentials_file: /etc/alertmanager/secrets/copilot-token
```

`send_resolved: true` importa: la resuelta cierra el ciclo (aviso de "se
resolvió" por los mismos canales a los que fue el disparo) y sin ella el
copiloto sólo ve el principio de cada incidente.

Response 202:

```json
{
  "received": 3,
  "queued": ["7e1a2b...", "c04d9f..."],
  "skipped": [{"fingerprint": "a91b33...", "decision": "deduped"}],
  "rejected": ["alerts[3]: sin alertname o sin fingerprint"]
}
```

- `queued`: fingerprints que entraron a procesar.
- `skipped`: las que no, con el motivo (vocabulario en §4.3).
- `rejected`: entradas del payload que no se pudieron leer. No hacen fallar
  el request: el resto del grupo se procesa igual.

Alertmanager reintenta el webhook si no recibe 2xx, así que un reinicio del
copiloto no pierde alertas. Un 401 acá es el token del `credentials_file`.

### `GET /v1/alerts`

Lo que llegó, qué se decidió y qué dijo el triage. Es lo que consulta la tool
`alerts_received` cuando alguien pregunta "¿qué pasó anoche?", y sirve para
que su tablero muestre el triage del modelo al lado de la alerta.

Query params: `limit` (default 50, tope 500), `only_firing` (bool).

```json
{
  "pending": 0,
  "total": 128,
  "note": "Persistido en /data.",
  "alerts": [
    {
      "fingerprint": "7e1a2b...",
      "name": "KubePodCrashLooping",
      "status": "firing",
      "severity": "critical",
      "labels": {"alertname": "KubePodCrashLooping", "namespace": "payments", "pod": "api-7f..."},
      "summary": "Pod payments/api-7f... is crash looping",
      "starts_at": "2026-09-14T03:12:07+00:00",
      "ends_at": null,
      "annotations": {"runbook_url": "..."},
      "source": "alertmanager",
      "received_at": "2026-09-14T03:12:41.120+00:00",
      "finished_at": "2026-09-14T03:13:19.804+00:00",
      "decision": "sent",
      "enrichment": {
        "expression": "rate(kube_pod_container_status_restarts_total[5m]) > 0",
        "duration_s": 34.2,
        "trend": [{"series": {"pod": "api-7f..."}, "first": 0.0, "last": 0.025,
                   "min": 0.0, "max": 0.025, "avg": 0.012}],
        "active_alerts": 2,
        "cost": null,
        "triage": "El pod reinicia cada ~40 s desde las 03:12 ...",
        "triage_tools": ["promql_range", "alerts_active"],
        "tokens": 2210,
        "errors": []
      },
      "deliveries": [{"channel": "guardia", "ok": true, "detail": "message_id=..."}]
    }
  ]
}
```

`enrichment` es `null` mientras la alerta sigue en `pending` o si se salteó;
`enrichment.errors` lista qué parte del enriquecimiento falló (por ejemplo
`"triage: TimeoutError: ..."`) sin que eso frene la entrega.

### 4.3 Vocabulario de `decision`

| `decision` | Significa | Se enriquece / avisa |
|---|---|---|
| `pending` | encolada, todavía procesándose | en curso |
| `sent` | se procesó y se entregó por al menos un canal | sí |
| `deduped` | ya se avisó de ese fingerprint hace menos de `repeat_interval` | no |
| `quiet` | horario de silencio y la severidad no lo saltea (`critical` sí) | no |
| `capped` | todos los canales llegaron a su `daily_cap` de hoy | no |
| `unrouted` | ningún canal acepta esa severidad (`severities:` en la config) | no |
| `no_channels` | no hay canales configurados | no |
| `unpaired` | una `resolved` cuyo `firing` nunca se avisó (o se avisó antes del último reinicio sin storage) | no |
| `inflight` | un `firing` repetido mientras el anterior del mismo fingerprint todavía se está enriqueciendo | no |
| `overloaded` | más de `alerts.queue_max` alertas en vuelo (default 200) | no |

`deduped` en un `firing` repetido es lo esperado: Alertmanager reenvía cada
`repeat_interval` suyo y el copiloto no vuelve a avisar hasta que pasa el
propio. Si ven muchas `overloaded`, está llegando una tormenta: subir
`queue_max` no es la solución; sí lo es filtrar en `matchers` o desactivar el
triage (`alerts.triage: false`), que es lo que tarda.

## 5. Las tools (qué puede mirar el agente)

`GET /v1/tools` devuelve el catálogo tal como se lo ve el modelo en ese
momento, con nombre, descripción y esquema de argumentos. Es lo primero a
mirar cuando "no usó tal tool": casi siempre es que no estaba en el catálogo
(por ejemplo, las de costo no aparecen si no hay `CostPort` configurado).

Todas son de lectura. Las 11 de esta versión:

| Tool | Fuente | Qué trae |
|---|---|---|
| `promql_instant` | Prometheus | un vector instantáneo |
| `promql_range` | Prometheus | una serie temporal, con `lookback` (`1h`, `24h`, ...) |
| `label_values` | Prometheus | valores de una label, para que el modelo descubra nombres |
| `metric_metadata` | Prometheus | tipo y help de una métrica |
| `targets_health` | Prometheus | targets up/down por job |
| `alerts_active` | Prometheus | reglas firing/pending ahora |
| `alert_rules` | Prometheus | reglas definidas |
| `alerts_received` | el propio copiloto | lo que llegó por `/v1/alerts` (§4) |
| `cost_daily` | costo (BSS o PromQL) | gasto por día |
| `cost_by_service` | costo | gasto por servicio/producto |
| `cost_by_resource` | costo | gasto por recurso |

El `budget` de `/v1/status` aplica a todas las de Prometheus. Si además del
Prometheus tienen Cloud Eye o el billing de Huawei, se enchufan como
adapters y las mismas tools los consultan; el modelo no distingue el
proveedor.

## 6. Auditoría: `GET /v1/audit`

Cada llamada a una tool queda registrada: quién (`actor`), qué tool, con qué
argumentos, cuánto tardó, si salió bien y, si no, el error. Es la respuesta a
"¿qué consultó el agente contra nuestros datos?".

Query params: `limit` (default 100, tope 500), `tool` (filtra por nombre),
`only_errors` (bool).

```json
{
  "summary": {"entries": 1532, "capacity": 1000, "errors": 4, "durable": true,
              "by_tool": {"promql_range": 812, "alerts_active": 301},
              "oldest": "2026-09-01T...", "newest": "2026-09-14T..."},
  "note": "Persistido en /data.",
  "entries": [
    {"ts": "2026-09-14T14:02:11+00:00", "actor": "slack:U0123ABC", "run_id": "…",
     "tool": "promql_range", "args": {"query": "...", "lookback": "6h"},
     "ok": true, "ms": 213, "result_chars": 4020, "error": "", "labels": {}}
  ]
}
```

`run_id` agrupa las tools de un mismo turno de chat o de un mismo triage. Si
`note` dice "Buffer en memoria", no hay `storage.path` configurado y la
auditoría se pierde al reiniciar: en producción configúrenlo (el chart lo
hace con el PVC).

## 7. Avisos y detectores (opcional)

Si además de contestar quieren que el copiloto **avise** (SMN, webhook a
Slack/Teams/lo que sea, o sólo log), se configuran canales y una política
en `notify:`. No es obligatorio: sin canales, el receiver igual enriquece y
`GET /v1/alerts` igual tiene el triage; sólo que nadie recibe un mensaje.

| Ruta | Para qué |
|---|---|
| `GET /v1/notify` | canales, política (dedup, quiet hours, tope, ruteo por severidad) y últimos mensajes |
| `POST /v1/notify/test` | manda un mensaje de prueba por todos los canales (`{"title","body","severity","force"}`) |
| `POST /v1/detectors/cost_spike/run` | evalúa ahora el detector de pico de gasto y, si hay, lo despacha con la política normal |

El detector de costo corre solo cada `detectors.cost_spike.interval` si hay
`CostPort`; el endpoint es para probarlo o para engancharlo a un cron propio.

## 8. Qué observar de la instancia

`/metrics` expone, entre otras:

- `copilot_agent_runs_total{status="done|budget|time|error"}`
- `copilot_agent_tokens_total{model}`
- `copilot_agent_steps_total{tool, ok}` y `copilot_tool_duration_seconds{tool}`
- `copilot_alerts_received_total{status}` y `copilot_alerts_processed_total{decision}`
- `copilot_port_up{port}` (1 = ok), lo que mira `/readyz`
- `copilot_notify_outcomes_total{decision}` y `copilot_notify_deliveries_total{channel, ok}`
- `copilot_http_requests_total{method, path, status}` y `copilot_http_request_duration_seconds`

Con eso alcanza para un panel y dos alertas: `copilot_port_up == 0` por
más de 5 minutos, y `increase(copilot_alerts_processed_total{decision="overloaded"}[10m]) > 0`.

## 9. Checklist de integración

1. `GET /readyz` → 200 con el token del Secret en `/v1/status`.
2. `POST /v1/chat` con una pregunta simple ("¿qué targets hay?") → `steps` no
   vacío y `text` coherente con lo que ven en su Prometheus.
3. Receiver configurado con `send_resolved: true`; forzar una alerta que ya
   tengan → aparece en `GET /v1/alerts` con `decision: "sent"` y
   `enrichment.triage` lleno; al resolverse, un segundo registro `resolved`.
4. `GET /v1/audit` muestra las tools de los pasos 2 y 3 con el `actor`
   correcto.
5. El cliente HTTP del bot tiene timeout > `agent.max_seconds`.
6. Lo que muestren al usuario incluye `steps` (o un link a ellos).
