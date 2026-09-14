# Instalar el copiloto en el cluster del cliente

Media tarde, si el cliente ya tiene Prometheus, Alertmanager y un endpoint de
modelo. Lo que hay que llevar: la imagen, el chart y un token. Lo que hay que
pedir: tres URLs y, si aplica, un AK/SK.

## 0. Qué hace falta tener antes

| | Dónde se usa |
|---|---|
| URL del Prometheus (o Thanos Query, Mimir, VictoriaMetrics) alcanzable desde el cluster | `env.PROMETHEUS_URL` |
| URL de un endpoint OpenAI-compatible (vLLM, Ollama, una API comercial) y el nombre del modelo | `env.MODEL_BASE_URL`, `env.MODEL_NAME`, `MODEL_API_KEY` en el Secret |
| Acceso para editar el `alertmanager.yml` del cliente (un receiver nuevo) | paso 5 |
| Un registry al que el cluster pueda hacer pull | `image.repository` |
| Si el cliente está en Huawei Cloud: un usuario IAM con [esta política](../deploy/iam/README.md) | `HW_AK`/`HW_SK` en el Secret |

Kubernetes ≥ 1.25, Helm 3, un StorageClass por defecto (1Gi).

## 1. Construir y publicar la imagen

```bash
docker build -t ghcr.io/<org>/observability-copilot:0.1.0 .
# con el SDK de Huawei adentro, sólo si hace falta:
docker build --build-arg EXTRAS=huawei -t ghcr.io/<org>/observability-copilot:0.1.0-huawei .
docker push ghcr.io/<org>/observability-copilot:0.1.0
```

Y el SBOM que va a pedir seguridad, con el escaneo de CVEs incluido:

```bash
IMAGE=ghcr.io/<org>/observability-copilot:0.1.0 IMAGE_PREBUILT=1 ./scripts/sbom.sh
# → dist/sbom.cdx.json (CycloneDX) y dist/sbom.txt
```

## 2. El Secret

Un solo Secret con todo lo que es secreto. El chart lo monta entero como
entorno y el `copilot.yaml` lo referencia con `${VAR}`.

```bash
kubectl create namespace copilot
kubectl -n copilot create secret generic copilot-secrets \
  --from-literal=COPILOT_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  --from-literal=MODEL_API_KEY='...'
  # --from-literal=HW_AK='...' --from-literal=HW_SK='...' --from-literal=SMN_TOPIC_URN='...'
```

Guardar el `COPILOT_API_TOKEN`: es el mismo que va a usar Alertmanager en el
paso 5 y el que se pone en el `Authorization: Bearer` para preguntar.

## 3. Los values

```yaml
# values-cliente.yaml
image:
  repository: ghcr.io/<org>/observability-copilot
  tag: "0.1.0"
existingSecret: copilot-secrets

env:
  PROMETHEUS_URL: http://prometheus-operated.monitoring.svc:9090
  MODEL_BASE_URL: http://vllm.ml.svc:8000/v1
  MODEL_NAME: qwen2.5-32b-instruct

config:
  server:
    api_token: ${COPILOT_API_TOKEN}
  metrics:
    adapter: prometheus
    url: ${PROMETHEUS_URL}
  model:
    adapter: openai_compat
    base_url: ${MODEL_BASE_URL}
    api_key: ${MODEL_API_KEY:-}
    model: ${MODEL_NAME}
  notify:
    - name: ops
      adapter: webhook          # o smn, o log mientras se prueba
      url: ${SLACK_WEBHOOK_URL}
    - name: guardia
      adapter: smn
      topic_urn: ${SMN_TOPIC_URN}   # la región sale del URN
      ak: ${HW_AK}
      sk: ${HW_SK}
      max_chars: 400
      severities: [critical]    # el SMS sólo para lo crítico; `ops` recibe todo
  dispatch:
    repeat_interval: 4h
    quiet_hours: {start: "23:00", end: "07:00", tz: America/Argentina/Buenos_Aires}
    daily_cap: 50
  alerts:
    triage: true
  storage:
    path: ${COPILOT_STORAGE_PATH:-}

networkPolicy:
  ingressFrom:
    - namespaceSelector:
        matchLabels: {kubernetes.io/metadata.name: monitoring}
  egressTo:
    - to:
        - namespaceSelector:
            matchLabels: {kubernetes.io/metadata.name: monitoring}
      ports: [{port: 9090, protocol: TCP}]
    - to:
        - namespaceSelector:
            matchLabels: {kubernetes.io/metadata.name: ml}
      ports: [{port: 8000, protocol: TCP}]
  # allowExternalHttps: true   # si el modelo o Huawei están fuera del cluster

serviceMonitor:
  enabled: true                # si hay prometheus-operator
  labels: {release: kube-prometheus-stack}
prometheusRule:
  enabled: true
  labels: {release: kube-prometheus-stack}
```

El bloque `config` es el `copilot.yaml` entero; todo lo que acepta está
comentado en [`config/copilot.example.yaml`](../config/copilot.example.yaml)
(Cloud Eye, BSS, SMN, el detector de gasto). Si el cliente está en Huawei,
sustituir `metrics`/`cost`/`notify` por los bloques `cloudeye`/`huawei_bss`/`smn`
de ese archivo.

## 4. Instalar y verificar

```bash
helm upgrade --install copilot deploy/helm/observability-copilot \
  -n copilot -f values-cliente.yaml
kubectl -n copilot rollout status deploy/copilot-observability-copilot
```

Y **antes de irse**:

```bash
kubectl -n copilot exec deploy/copilot-observability-copilot -- python -m copilot preflight
kubectl -n copilot exec deploy/copilot-observability-copilot -- python -m copilot notify-test
```

Antes de instalar en el cluster del cliente, `scripts/verify-kind.sh` hace
todo esto en un kind local con Calico: `helm install`, la NetworkPolicy
cortando de verdad (desde `monitoring` llega, desde `default` no), el PVC
sobreviviendo al pod, Alertmanager real contra el receiver y el chat con el
modelo. Tarda unos minutos y borra el cluster al final.

Desde afuera del cluster, con el `.env` completo y `config/copilot.yaml`
apuntando a las credenciales reales, `scripts/verify-real.sh` corre lo mismo y
además cada tool de lectura y el detector en seco; con `--notify` manda el
mensaje de prueba y con `--ask "..."` una pregunta al modelo. No imprime
secretos.

`preflight` toca cada puerto con las credenciales reales y dice, uno por uno,
qué anda. `notify-test` manda un mensaje por cada canal salteando dedup, quiet
hours, tope y ruteo por severidad: si no llega, no es la política, es el canal.

Si `preflight` falla en `metrics` con timeout, casi siempre es la
NetworkPolicy: el selector de `egressTo` no matchea el namespace o el pod del
Prometheus. Ver con `kubectl -n copilot describe networkpolicy`.

## 5. El receiver en el Alertmanager del cliente

En **su** `alertmanager.yml` (o en el `AlertmanagerConfig` si usan el
operator), un receiver más. No se toca su ruteo existente: el copiloto se
agrega como `continue: true` para que lo que ya llega a PagerDuty siga
llegando.

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

El archivo con el token es el mismo `COPILOT_API_TOKEN`; con el operator se
monta desde un Secret con `spec.secrets`. Con el operator, la NetworkPolicy
del chart ya deja entrar al namespace `monitoring`; si Alertmanager vive en
otro, agregarlo a `networkPolicy.ingressFrom`.

Verificar la cadena entera provocando una alerta que ya tengan (o bajando un
target de prueba) y mirando:

```bash
kubectl -n copilot port-forward svc/copilot-observability-copilot 8080:8080
curl -s -H "Authorization: Bearer $COPILOT_API_TOKEN" 'localhost:8080/v1/alerts?limit=5'
```

Tiene que aparecer con `"decision": "sent"` y, si `triage` está activo, con
las seis líneas del modelo en `enrichment.triage`.

## 6. Preguntar

```bash
curl -X POST localhost:8080/v1/chat \
  -H "Authorization: Bearer $COPILOT_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"¿qué está firing ahora y desde cuándo?"}'
```

La respuesta trae el texto y la traza de tools. Todo lo que se consultó queda
en `GET /v1/audit`, persistido en el PVC.
El contrato completo de la API (chat, receiver, vocabulario de decisiones,
auditoría) está en [`integracion.md`](integracion.md).

## Operación

- **Actualizar**: `helm upgrade` con la imagen nueva. La estrategia es
  `Recreate` (el PVC es RWO): segundos de corte, Alertmanager reintenta el
  webhook, no se pierde nada.
- **Volver atrás**: `helm rollback copilot`. El estado en el PVC es
  compatible hacia atrás dentro de la misma versión mayor.
- **Cambiar la config**: editar los values y `helm upgrade`; el checksum del
  ConfigMap reinicia el pod solo.
- **Rotar el token**: actualizar el Secret, reiniciar el pod
  (`kubectl rollout restart`), y cambiar el archivo que lee Alertmanager.
- **Qué mirar**: `/metrics` del copiloto (turnos, tools, alertas recibidas,
  entregas por canal, tokens) y las tres reglas del `PrometheusRule`: caído,
  entregas fallando, tope diario alcanzado.
- **Qué NO hace**: no escribe en Prometheus, no toca la API de Kubernetes
  (`automountServiceAccountToken: false`), no sale a ningún lado que la
  NetworkPolicy no liste, no guarda resultados de queries (sólo qué se
  preguntó). Es lo que hay que decirle a seguridad, y todo está en el chart
  para que lo lean.

## Desinstalar

```bash
helm -n copilot uninstall copilot
kubectl -n copilot delete pvc copilot-observability-copilot-data   # la auditoría se va con esto
kubectl -n copilot delete secret copilot-secrets
```

Y sacar el receiver del `alertmanager.yml`.
