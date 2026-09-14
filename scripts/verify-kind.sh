#!/usr/bin/env bash
# El chart en un cluster de verdad, antes de que lo instale el cliente.
#
# Levanta un kind con Calico (el CNI por defecto de kind NO aplica
# NetworkPolicy, y la policy default-deny es justamente lo que hay que ver
# funcionando), un Prometheus y un Alertmanager de juguete en el namespace
# `monitoring`, y el copiloto con `helm install` en `copilot`. Después:
#
#   - preflight adentro del pod: llega al Prometheus (egress a monitoring) y al
#     modelo (egress 443 afuera), con la policy puesta.
#   - Alertmanager real → /v1/alerts (ingress desde monitoring).
#   - un pod en `default` NO llega al copiloto (la policy corta lo demás).
#   - se borra el pod y lo recibido sigue estando (PVC).
#   - /v1/chat con el modelo, si hay MODEL_BASE_URL en .env.
#
# Necesita docker, kind, helm y kubectl. Tarda unos minutos (Calico). No
# imprime secretos. Al terminar borra el cluster, salvo KEEP=1.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER="${CLUSTER:-copilot-verify}"
IMG="observability-copilot:kind"
NS=copilot
REL=copilot
FULL="$REL-observability-copilot"
LOCAL_PORT="${LOCAL_PORT:-18081}"
CALICO="https://raw.githubusercontent.com/projectcalico/calico/v3.29.1/manifests/calico.yaml"

ok()   { printf '  \033[32m[OK]\033[0m    %s\n' "$1"; }
fail() { printf '  \033[31m[FALLA]\033[0m %s\n' "$1"; exit 1; }
skip() { printf '  \033[33m[SALTEA]\033[0m %s\n' "$1"; }

# Sólo estas claves salen del .env, y nunca a la pantalla.
if [ -f .env ]; then
  for k in MODEL_BASE_URL MODEL_API_KEY MODEL_NAME; do
    [ -n "${!k:-}" ] || export "$k"="$(grep -E "^$k=" .env | cut -d= -f2- | tr -d '[:space:]' || true)"
  done
fi
TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
SCRATCH="$(mktemp -d)"
PF_PID=""

cleanup() {
  if [ -n "$PF_PID" ]; then kill "$PF_PID" 2>/dev/null; wait "$PF_PID" 2>/dev/null; fi
  rm -rf "$SCRATCH"
  if [ "${KEEP:-0}" = 1 ]; then
    echo "KEEP=1: el cluster '$CLUSTER' queda arriba (kind delete cluster --name $CLUSTER)"
  else
    kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "→ Cluster kind '$CLUSTER' con Calico"
cat > "$SCRATCH/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true
  podSubnet: 192.168.0.0/16
EOF
kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
kind create cluster --name "$CLUSTER" --config "$SCRATCH/kind.yaml" >/dev/null 2>&1
kubectl apply -f "$CALICO" >/dev/null
kubectl -n kube-system rollout status ds/calico-node --timeout=600s >/dev/null
kubectl -n kube-system rollout status deploy/calico-kube-controllers --timeout=600s >/dev/null
kubectl -n kube-system rollout status deploy/coredns --timeout=120s >/dev/null
ok "cluster arriba, Calico aplicando NetworkPolicy"

echo "→ Imágenes → kind (la del copiloto se construye; las otras se bajan una vez)"
docker build -q -t "$IMG" . >/dev/null
# `kind load docker-image` falla con el image store de containerd de Docker
# Desktop (manifiestos multi-plataforma: "content digest not found"). Un
# archive de una sola plataforma entra siempre.
PLAT="linux/$(docker version --format '{{.Server.Arch}}')"
carga() {
  docker image inspect "$1" >/dev/null 2>&1 || docker pull -q "$1" >/dev/null
  docker save --platform "$PLAT" "$1" -o "$SCRATCH/img.tar"
  kind load image-archive "$SCRATCH/img.tar" --name "$CLUSTER" >/dev/null 2>&1
  rm -f "$SCRATCH/img.tar"
}
for img in "$IMG" prom/prometheus:v3.1.0 prom/alertmanager:v0.28.1 curlimages/curl:8.11.1; do
  carga "$img" || fail "no se pudo cargar $img en el nodo"
done
ok "imágenes cargadas en el nodo"

echo "→ Prometheus y Alertmanager de juguete en 'monitoring'"
kubectl create namespace monitoring >/dev/null
cat > "$SCRATCH/prometheus.yml" <<EOF
global: {scrape_interval: 5s, evaluation_interval: 5s}
rule_files: [/etc/prometheus/alerts.yml]
alerting:
  alertmanagers: [{static_configs: [{targets: ["alertmanager.monitoring.svc:9093"]}]}]
scrape_configs:
  - job_name: prometheus
    static_configs: [{targets: ["localhost:9090"]}]
  - job_name: caido_a_proposito
    static_configs: [{targets: ["no-existe.invalid:9100"]}]
EOF
cat > "$SCRATCH/alertmanager.yml" <<EOF
route: {receiver: copilot, group_wait: 5s, group_interval: 10s, repeat_interval: 1h}
receivers:
  - name: copilot
    webhook_configs:
      - url: http://$FULL.$NS.svc:8080/v1/alerts
        send_resolved: true
        http_config: {authorization: {credentials_file: /etc/alertmanager/token}}
EOF
kubectl -n monitoring create configmap prometheus \
  --from-file=prometheus.yml="$SCRATCH/prometheus.yml" \
  --from-file=alerts.yml=deploy/prometheus/alerts.yml >/dev/null
kubectl -n monitoring create configmap alertmanager \
  --from-file=alertmanager.yml="$SCRATCH/alertmanager.yml" >/dev/null
kubectl -n monitoring create secret generic copilot-token --from-literal=token="$TOKEN" >/dev/null
kubectl -n monitoring apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: prometheus}
spec:
  selector: {matchLabels: {app: prometheus}}
  template:
    metadata: {labels: {app: prometheus}}
    spec:
      containers:
        - name: prometheus
          image: prom/prometheus:v3.1.0
          args: [--config.file=/etc/prometheus/prometheus.yml, --storage.tsdb.retention.time=1d]
          ports: [{containerPort: 9090}]
          volumeMounts: [{name: cfg, mountPath: /etc/prometheus}]
      volumes: [{name: cfg, configMap: {name: prometheus}}]
---
apiVersion: v1
kind: Service
metadata: {name: prometheus}
spec: {selector: {app: prometheus}, ports: [{port: 9090, targetPort: 9090}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: alertmanager}
spec:
  selector: {matchLabels: {app: alertmanager}}
  template:
    metadata: {labels: {app: alertmanager}}
    spec:
      containers:
        - name: alertmanager
          image: prom/alertmanager:v0.28.1
          args: [--config.file=/etc/alertmanager/alertmanager.yml]
          ports: [{containerPort: 9093}]
          volumeMounts:
            - {name: cfg, mountPath: /etc/alertmanager/alertmanager.yml, subPath: alertmanager.yml}
            - {name: token, mountPath: /etc/alertmanager/token, subPath: token}
      volumes:
        - {name: cfg, configMap: {name: alertmanager}}
        - {name: token, secret: {secretName: copilot-token}}
---
apiVersion: v1
kind: Service
metadata: {name: alertmanager}
spec: {selector: {app: alertmanager}, ports: [{port: 9093, targetPort: 9093}]}
EOF
kubectl -n monitoring rollout status deploy/prometheus --timeout=300s >/dev/null \
  || { kubectl -n monitoring get pods; kubectl -n monitoring describe pod -l app=prometheus | tail -15; fail "prometheus no levantó"; }
kubectl -n monitoring rollout status deploy/alertmanager --timeout=300s >/dev/null \
  || { kubectl -n monitoring get pods; kubectl -n monitoring describe pod -l app=alertmanager | tail -15; fail "alertmanager no levantó"; }
ok "Prometheus y Alertmanager corriendo"

echo "→ helm install del copiloto en '$NS'"
kubectl create namespace "$NS" >/dev/null
kubectl -n "$NS" create secret generic copilot-secrets \
  --from-literal=COPILOT_API_TOKEN="$TOKEN" \
  --from-literal=MODEL_API_KEY="${MODEL_API_KEY:-}" >/dev/null
cat > "$SCRATCH/values.yaml" <<EOF
image: {repository: observability-copilot, tag: kind, pullPolicy: Never}
existingSecret: copilot-secrets
env:
  PROMETHEUS_URL: http://prometheus.monitoring.svc:9090
  MODEL_BASE_URL: ${MODEL_BASE_URL:-http://sin-modelo.invalid/v1}
  MODEL_NAME: ${MODEL_NAME:-sin-modelo}
  COPILOT_LOG_FORMAT: text
networkPolicy:
  allowExternalHttps: true     # el modelo está afuera del cluster
EOF
helm install "$REL" deploy/helm/observability-copilot -n "$NS" -f "$SCRATCH/values.yaml" >/dev/null
kubectl -n "$NS" rollout status deploy/"$FULL" --timeout=300s >/dev/null \
  || { kubectl -n "$NS" get pods; kubectl -n "$NS" describe pod -l app.kubernetes.io/instance="$REL" | tail -20; kubectl -n "$NS" logs deploy/"$FULL" --tail=30; fail "el copiloto no levantó"; }
ok "helm install: el pod está Ready (readOnlyRootFilesystem, no-root, PVC montado)"

echo "→ Verificaciones"
kubectl -n "$NS" get networkpolicy "$FULL" >/dev/null || fail "no se creó la NetworkPolicy"
kubectl -n "$NS" get pvc "$FULL-data" -o jsonpath='{.status.phase}' | grep -q Bound || fail "el PVC no está Bound"
ok "NetworkPolicy y PVC (Bound) en su lugar"

salida=$(kubectl -n "$NS" exec deploy/"$FULL" -- python -m copilot preflight 2>&1 || true)
echo "$salida" | grep -q '\[OK   \] metrics' || fail "preflight no llega al Prometheus (egress a monitoring): $salida"
ok "preflight: llega al Prometheus de 'monitoring' con la policy puesta"
if [ -n "${MODEL_BASE_URL:-}" ]; then
  echo "$salida" | grep -q '\[OK   \] model' || fail "preflight no llega al modelo (egress 443 afuera): $salida"
  ok "preflight: llega al modelo afuera del cluster (allowExternalHttps)"
fi

kubectl -n "$NS" port-forward svc/"$FULL" "$LOCAL_PORT":8080 >/dev/null 2>&1 &
PF_PID=$!
APP="http://localhost:$LOCAL_PORT"
for _ in $(seq 1 30); do curl -sf "$APP/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -sf "$APP/healthz" >/dev/null || fail "no se pudo hacer port-forward"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$APP/v1/status")" = 401 ] || fail "/v1/status sin token no dio 401"
curl -sf -H "Authorization: Bearer $TOKEN" "$APP/v1/status" | grep -q '"durable": *true' \
  || fail "/v1/status no reporta storage durable"
ok "API con token, storage durable en el PVC"

echo "→ Alertmanager real → /v1/alerts, a través de la NetworkPolicy"
for _ in $(seq 1 90); do
  salida=$(curl -sf -H "Authorization: Bearer $TOKEN" "$APP/v1/alerts?limit=5" || true)
  echo "$salida" | grep -q '"decision": *"sent"' && break
  sleep 1
done
echo "$salida" | grep -q '"name": *"TargetCaido"' || fail "no llegó TargetCaido: $salida"
echo "$salida" | grep -q '"decision": *"sent"' || fail "la alerta no se entregó: $salida"
ok "TargetCaido llegó desde 'monitoring', se enriqueció y se entregó"

echo "→ Un pod en 'default' NO tiene que llegar al copiloto"
codigo=$(kubectl -n default run np-probe --rm -i --restart=Never --image=curlimages/curl:8.11.1 \
  --command -- curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://$FULL.$NS.svc:8080/healthz" 2>/dev/null || true)
if echo "$codigo" | grep -q '200'; then
  fail "un pod de 'default' llegó al copiloto: la NetworkPolicy no está cortando"
fi
ok "default-deny: desde 'default' no se llega (Calico aplica la policy)"

echo "→ Borrar el pod: lo recibido tiene que seguir en el PVC"
kubectl -n "$NS" delete pod -l app.kubernetes.io/instance="$REL" --wait=true >/dev/null
kubectl -n "$NS" rollout status deploy/"$FULL" --timeout=180s >/dev/null
kill "$PF_PID" 2>/dev/null || true
kubectl -n "$NS" port-forward svc/"$FULL" "$LOCAL_PORT":8080 >/dev/null 2>&1 &
PF_PID=$!
for _ in $(seq 1 30); do curl -sf "$APP/healthz" >/dev/null 2>&1 && break; sleep 1; done
salida=$(curl -sf -H "Authorization: Bearer $TOKEN" "$APP/v1/alerts?limit=5")
echo "$salida" | grep -q '"name": *"TargetCaido"' || fail "la alerta no sobrevivió al pod nuevo: $salida"
ok "pod nuevo, misma alerta: el PVC funciona"

if [ -n "${MODEL_BASE_URL:-}" ]; then
  echo "→ /v1/chat con el modelo, desde adentro del cluster"
  respuesta=$(curl -sf -X POST "$APP/v1/chat" -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"message":"¿cuántos targets están up? nombrá el que esté caído"}')
  echo "$respuesta" | grep -q '"tool": *"targets_health"' || fail "el modelo no usó la tool: $respuesta"
  ok "/v1/chat contestó usando la tool"
else
  skip "/v1/chat — sin MODEL_BASE_URL"
fi

echo
echo "Chart verificado en un cluster real."
