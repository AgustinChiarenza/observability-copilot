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
```

## Configuración

Todo en un YAML que el cliente puede versionar en su repo de infra, sin un solo
secreto: cualquier valor acepta `${VAR}` o `${VAR:-default}` y se resuelve contra
el entorno. Un `${VAR}` sin valor y sin default **corta el arranque** nombrando el
campo — mejor eso que un 401 raro tres horas después.

Ver [`config/copilot.example.yaml`](config/copilot.example.yaml), que está
comentado entero.

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
  api/          FastAPI: /v1/chat, /v1/status, /v1/tools, /v1/audit, salud.
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
| **Hecho** | puertos, registro de adapters, config validada al arranque, adapter de Prometheus con presupuesto, adapter de costo por PromQL, adapter de modelo OpenAI-compatible, canales log y webhook, loop del agente, 7 tools de lectura, auditoría, API con auth, métricas propias, imagen y compose |
| **F1** | el adapter de métricas contra Thanos/Mimir/VictoriaMetrics en CI, mTLS, SigV4 |
| **F2** | `POST /v1/alerts` con el esquema de Alertmanager, enriquecimiento y triage |
| **F3** | canales de verdad: SMN/SMS, Slack con bloques, Teams, mail — con dedup, quiet hours y tope diario |
| **F4** | detector de pico de gasto |
| **F5** | análisis declarativos en YAML |
| **F6** | bot de Slack y Teams |
| **F7** | Helm, NetworkPolicy, SBOM, política IAM read-only, guía de instalación |

### El criterio de salida, verificado

> docker run + un Prometheus de juguete → `/v1/chat` contesta cuántos targets están up.

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
