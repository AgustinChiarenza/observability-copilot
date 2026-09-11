# Copilot — addon de observabilidad

Se enchufa al stack que el cliente **ya tiene**. Lee sus métricas por PromQL,
recibe las alertas que su Alertmanager ya evalúa, consulta su costo de donde ya
esté, y contesta preguntas sobre todo eso.

No trae TSDB. No trae motor de reglas. No toca infraestructura.

```
   El stack del cliente                    Este contenedor
 ┌────────────────────────┐   PromQL    ┌──────────────────────────────┐
 │ Prometheus / Thanos /  │◀────────────│ MetricsPort                  │
 │ Mimir / VictoriaMetrics│             │                              │
 ├────────────────────────┤   webhook   │                              │
 │ Alertmanager           │────────────▶│ AlertIngress        (F2)     │
 ├────────────────────────┤             │                              │
 │ Su gasto, donde esté   │◀────────────│ CostPort                     │
 ├────────────────────────┤  OpenAI API │                              │
 │ Su endpoint de LLM     │◀────────────│ ModelPort                    │
 ├────────────────────────┤             │                              │
 │ Slack / Teams / SMN    │◀────────────│ NotifyPort                   │
 └────────────────────────┘             └──────────────────────────────┘
```

## Por qué está armado así

**No compite con lo que ya tienen.** El cliente tiene reglas de Prometheus y un
Alertmanager que funciona, con `for:`, inhibición, silences y dedup en HA.
Escribir otro evaluador de umbrales sería competir contra un componente en el
que ya confían, con menos funcionalidad y sin su historial. Este producto recibe
lo que ese Alertmanager ya decidió y le agrega lo que él no puede hacer:
contexto, correlación con costo y una explicación.

**El costo se lee de donde ya está.** El adapter por defecto no va a una API de
facturación: corre PromQL contra la misma TSDB de las métricas. Si el cliente ya
exporta su gasto —kubecost, opencost, un exporter de billing, un recording rule
sobre la factura— el dato ya está en casa: responde en milisegundos, no pide
credenciales nuevas y no tiene rate limit.

**El modelo es el de ellos.** El ModelPort habla el dialecto OpenAI de chat
completions, que implementan vLLM, Ollama, TGI, LiteLLM, MaaS, Azure OpenAI y
cualquier gateway corporativo. Los datos no salen de su red, que es la diferencia
entre una instalación y un proceso de aprobación de tres meses.

**Sólo lee.** No existe una tool de escritura en el binario — no está apagada,
no está. Ver [`copilot/agent/permissions.py`](copilot/agent/permissions.py), que
son 60 líneas y se puede auditar de una sentada.

## Arrancar

```bash
cp config/copilot.example.yaml config/copilot.yaml
cp .env.example .env          # los secretos van acá, nunca en el YAML
docker compose up -d
```

Verificar que quedó bien enchufado, **antes de irte de lo del cliente**:

```bash
docker compose exec copilot python -m copilot preflight
```

```
  [OK   ] metrics            ok
  [OK   ] cost               ok
  [FALLA] model              ModelError: no se pudo alcanzar http://vllm:8000/v1
  [OK   ] notify:ops         ok
```

Preguntar algo:

```bash
curl -X POST http://localhost:8080/v1/chat \
  -H "Authorization: Bearer $COPILOT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"¿cuántos targets están up? nombrá el que esté caído"}'
```

La respuesta trae el texto **y la traza**: qué tools se corrieron, con qué
argumentos y cuánto tardaron. Un agente de observabilidad que contesta un número
sin mostrar de dónde salió es un agente en el que nadie confía la segunda vez.

Correr una tool sola, sin gastar un token ni depender del LLM:

```bash
docker compose exec copilot python -m copilot tool promql_instant '{"query":"up"}'
docker compose exec copilot python -m copilot tool alerts_active '{}'
```

Las alertas se leen del mismo backend de métricas (`/api/v1/alerts` y
`/api/v1/rules`, que exponen Prometheus, vmalert y los Ruler de Thanos/Mimir):
qué está firing, desde cuándo, y la expresión de la regla que lo disparó —que
es la query para correr en rango y explicarlo. Es lo que el backend **evalúa**,
no lo que **suena**: silences e inhibición son de Alertmanager y llegan en F2.

## Las alertas que ya suenan

El Alertmanager del cliente ya tiene `for:`, inhibición, silences y dedup en
HA. No se compite con eso: se le agrega un receiver.

```yaml
receivers:
  - name: copilot
    webhook_configs:
      - url: http://copilot:8080/v1/alerts
        send_resolved: true
        http_config:
          authorization: {credentials_file: /etc/alertmanager/copilot-token}
```

`POST /v1/alerts` contesta en milisegundos qué encoló y qué salteó — decide
primero con la misma política de siempre (dedup por el `fingerprint` de
Alertmanager, quiet hours, tope) y **sólo lo que va a salir se enriquece**,
atrás y de a pocos. Una alerta repetida no gasta un token.

Lo que se le agrega es lo que Alertmanager no puede: la expresión de la regla
(se busca en el backend por nombre; el webhook no la trae), cómo venía esa
expresión antes del disparo, cuántas otras hay firing, el gasto del día, y —si
hay modelo— seis líneas de triage con el recurso concreto, la hipótesis y qué
mirar primero. Cada pieza falla sola: una alerta que no llega porque el
enriquecimiento explotó es peor que una alerta pelada.

`GET /v1/alerts` y la tool `alerts_received` muestran lo que **sonó** y qué se
dijo de cada una — a diferencia de `alerts_active`, que es lo que el backend
evalúa antes de silences. "¿Qué pasó anoche?" se contesta con la primera.

## Si el cliente está en Huawei Cloud

"Lo del Cloud Eye y el BSS el cliente ya lo tiene": dos adapters, cero
cambios en el core, y el SDK sólo entra si se pide (`pcnt-copilot[huawei]` o
`--build-arg EXTRAS=huawei`).

- **`metrics: cloudeye`** — Cloud Eye no habla PromQL y el adapter no lo
  finge. `query` es un selector `SYS.ECS/cpu_util{instance_id="..."}`, sin
  funciones; sin dimensiones consulta todos los recursos que reportan esa
  métrica, acotado por el presupuesto. El adapter declara `query_syntax` y eso
  entra al prompt del sistema, así que el modelo escribe lo que el backend
  entiende sin que el core sepa cuál es. Las alarmas salen de las reglas y del
  historial de Cloud Eye; `targets_health` desaparece del catálogo porque no
  hay targets de scrape — mejor que contestar "0 de 0".
- **`cost: huawei_bss`** — los fee records de BSS agrupados por día, servicio
  y recurso. Va un día atrás (`lag_days: 1`), se pagina en paralelo y se
  recuerda diez minutos: el agente pregunta tres veces por lo mismo en un
  turno y BSS no tiene por qué enterarse tres veces. Sigue siendo el camino
  secundario: si el gasto ya está en la TSDB, `promql` llega antes y sin
  credenciales nuevas.

Los dos están probados contra clientes falsos con los modelos reales del SDK.
Contra la nube de verdad los prueba `copilot preflight` con las credenciales
del cliente, que es donde corresponde.

## Alarmas de gasto

El agente contesta; el detector avisa solo. Corre dentro del mismo contenedor,
cada `interval`, y aplica una regla que está en código y no en un prompt:

> último día completo / mediana de la ventana ≥ `threshold`

Mediana y no promedio, para que un pico anterior no tape el siguiente. "Último
día completo" respeta el `lag_days` del costo: el día que todavía se está
llenando siempre parece raro, y es la falsa alarma más común de FinOps. El
aviso sale con el desglose por servicio de ese día, que es lo que lo hace
accionable.

```yaml
detectors:
  cost_spike: {enabled: true, interval: 1h, window_days: 14, threshold: 1.5}
```

Lo que sale pasa por **una** política, igual para todos los canales, en
[`copilot/dispatch.py`](copilot/dispatch.py): el mismo aviso no se repite antes
de `repeat_interval`, lo que no es crítico espera fuera de `quiet_hours`, y hay
un `daily_cap` por canal que al alcanzarse manda un último aviso y calla hasta
el día siguiente. Es la línea que separa "un bug en el detector" de "200 SMS
una madrugada".

Canales: `log`, `webhook` (Slack, Teams, lo que reciba JSON) y `smn` de Huawei
(SMS, mail o HTTP, lo que tenga suscripto el topic). SMN es la primera pieza
con SDK propio y por eso es un extra: `pip install pcnt-copilot[huawei]`, o la
imagen con `--build-arg EXTRAS=huawei`. La imagen genérica no lo lleva.

Antes de irte de lo del cliente:

```bash
docker compose exec copilot python -m copilot notify-test          # ¿llega?
docker compose exec copilot python -m copilot detect cost_spike --dry-run
```

Lo mismo por API: `POST /v1/notify/test`, `POST /v1/detectors/cost_spike/run`
y `GET /v1/notify` para ver qué pasó con los últimos mensajes y por qué.

## Configuración

Todo en un YAML que el cliente puede versionar en su repo de infra, sin un solo
secreto: cualquier valor acepta `${VAR}` o `${VAR:-default}` y se resuelve contra
el entorno. Un `${VAR}` sin valor y sin default **corta el arranque** nombrando el
campo — mejor eso que un 401 raro tres horas después.

Ver [`config/copilot.example.yaml`](config/copilot.example.yaml), que está
comentado entero.

### Lo que se persiste, y por qué tan poco

No hay base de datos. Con `storage.path` apuntando a un volumen, quedan tres
archivos: la auditoría (qué se consultó), las alertas recibidas (qué sonó y qué
se dijo de cada una) y el estado del despachante. El último es el que importa:
sin él, un reinicio a las 3 AM olvida el dedup y el tope diario, y lo primero que
hace el proceso nuevo es re-mandar lo que el viejo ya había frenado. El compose
monta ese volumen; el contenedor sigue siendo read-only fuera de él. Sin
`storage.path` todo queda en memoria y `/v1/status` lo dice
(`storage.durable: false`).

### El presupuesto no es opcional

```yaml
budget:
  max_series: 500
  max_points: 11000
  max_range: 31d
  timeout: 30s
```

Es lo único que separa "el agente consulta las métricas del cliente" de "el
agente le puede tirar abajo el Prometheus de producción". El `step` de un range
lo calcula el adapter respetando `max_points` —un modelo pidiendo 30 días con
step de 15s son 172.800 puntos por serie—, lo que pase `max_series` se descarta
**marcado como parcial**, y un selector sin matchers se rechaza antes de salir a
la red.

## Qué hay adentro

```
copilot/
  ports/        los Protocol. Es lo único que conoce el core.
  adapters/     lo específico de cada backend. Se registran con un decorador.
  agent/        loop con presupuesto, catálogo de tools, permisos, auditoría.
  alerts/       el webhook de Alertmanager → decidir → enriquecer → entregar.
  detectors/    lo que corre solo y avisa. Hoy: pico de gasto.
  dispatch.py   la política de entrega, una para todos los canales.
  store.py      JSONL y un JSON en un volumen: lo poco que sobrevive al reinicio.
  api/          FastAPI: /v1/chat, /v1/alerts, /v1/notify, /v1/status, /v1/tools, /v1/audit, salud.
  telemetry/    las métricas del propio copiloto, en /metrics.
```

**La regla que sostiene todo:** fuera de `copilot/adapters/` no se importa el SDK
de ningún proveedor. El día que un `import huaweicloudsdk*` aparezca en el core,
el producto dejó de ser enchufable y nadie se va a enterar hasta el segundo
cliente.

Agregar un backend es escribir un archivo en `adapters/` con un decorador
`@register("metrics", "loquesea")` y una línea de YAML. Si alguna vez hay que
editar un `if` en el core para sumar uno, el diseño se rompió.

## Estado: F0 terminado

| | |
|---|---|
| **Hecho** | puertos, registro de adapters, config validada al arranque, adapters de métricas Prometheus y Cloud Eye con presupuesto, adapters de costo PromQL y BSS, adapter de modelo OpenAI-compatible, loop del agente, 11 tools de lectura (métricas con su metadata, alertas y costo), `POST /v1/alerts` con el esquema de Alertmanager, enriquecimiento y triage, auditoría, alertas y estado del despachante persistidos en un volumen, API con auth, métricas propias, imagen y compose; **detector de pico de gasto** con despachante (dedup, quiet hours, tope diario) y canales log, webhook y SMN |
| **F1** | el adapter de métricas contra Thanos/Mimir/VictoriaMetrics en CI, mTLS, SigV4 |
| **F2** | ruteo por severidad a canales distintos |
| **F3** | más canales: Slack con bloques, Teams, mail |
| **F5** | análisis declarativos en YAML |
| **F6** | bot de Slack y Teams |
| **F7** | Helm, NetworkPolicy, SBOM, política IAM read-only, guía de instalación |

### El criterio de salida, verificado

> docker run + un Prometheus de juguete → `/v1/chat` contesta cuántos targets están up.

Y desde F0.4, además: un Alertmanager de juguete dispara sobre el target caído
y la alerta llega por `/v1/alerts`, enriquecida con la regla y entregada.

```bash
./scripts/verify-f0.sh    # con MODEL_BASE_URL exportado, prueba el turno completo
```

Contra contenedores reales y un LLM real, el agente contesta:

> Hay **2 targets up** de 3 totales. El que está caído: job `caido_a_proposito`,
> instance `no-existe.invalid:9100`, error `dial tcp: lookup no-existe.invalid:
> no such host` — no resuelve el DNS, el host literalmente no existe.

…habiendo llamado `targets_health` una vez, con la traza en la respuesta.

Además, [`tests/test_exit_criterion.py`](tests/test_exit_criterion.py) corre la
misma cadena en CI con el transporte falseado en el último salto.

## Desarrollo

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check copilot tests
```

Los comentarios están en español y los identificadores en inglés: el repo se
instala en el stack de un tercero y lo van a leer sus SRE, pero lo mantenemos
nosotros.
