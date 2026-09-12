#!/usr/bin/env bash
# El chart, sin cluster: lint, render con las variantes que cambian de forma y
# validación de cada manifiesto contra el esquema de Kubernetes. Y lo que más
# importa: que el copilot.yaml que sale del ConfigMap sea uno que el copiloto
# de verdad acepta — un chart que rinde YAML válido y config inválida se
# descubre en lo del cliente.
set -euo pipefail
cd "$(dirname "$0")/.."
CHART=deploy/helm/observability-copilot
PY="${PY:-.venv/bin/python}"
command -v helm >/dev/null || { echo "falta helm"; exit 1; }
command -v kubeconform >/dev/null || { echo "falta kubeconform (brew install kubeconform)"; exit 1; }

ok()   { printf '  \033[32m[OK]\033[0m    %s\n' "$1"; }
fail() { printf '  \033[31m[FALLA]\033[0m %s\n' "$1"; exit 1; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

helm lint "$CHART" --set existingSecret=s --quiet >/dev/null || fail "helm lint"
ok "helm lint"

# Sin Secret no se instala: es el error que tiene que aparecer, con nombre.
if helm template t "$CHART" >/dev/null 2>"$tmp/err"; then fail "sin existingSecret debería fallar"; fi
grep -q "existingSecret es requerido" "$tmp/err" || fail "el error no nombra el Secret"
ok "sin existingSecret falla con nombre"

variantes=(
  "minima|--set existingSecret=s"
  "completa|--set existingSecret=s --set serviceMonitor.enabled=true --set prometheusRule.enabled=true --set ingress.enabled=true --set networkPolicy.allowExternalHttps=true"
  "sin-persistencia|--set existingSecret=s --set persistence.enabled=false --set networkPolicy.enabled=false"
)
for v in "${variantes[@]}"; do
  nombre="${v%%|*}"; args="${v#*|}"
  # shellcheck disable=SC2086
  helm template t "$CHART" $args > "$tmp/$nombre.yaml" || fail "render $nombre"
  kubeconform -strict -ignore-missing-schemas -summary "$tmp/$nombre.yaml" > "$tmp/$nombre.out" \
    || { cat "$tmp/$nombre.out"; fail "kubeconform $nombre"; }
  ok "variante $nombre: $(grep -o 'Valid: [0-9]*' "$tmp/$nombre.out"), $(grep -o 'Invalid: [0-9]*' "$tmp/$nombre.out")"
done

grep -q 'readOnlyRootFilesystem: true' "$tmp/minima.yaml" || fail "el contenedor no es read-only"
grep -q 'automountServiceAccountToken: false' "$tmp/minima.yaml" || fail "monta el token de SA"
grep -q 'kind: NetworkPolicy' "$tmp/minima.yaml" || fail "no hay NetworkPolicy por defecto"
grep -q 'kind: PersistentVolumeClaim' "$tmp/minima.yaml" || fail "no hay PVC por defecto"
! grep -q 'persistentVolumeClaim' "$tmp/sin-persistencia.yaml" || fail "sin persistencia igual monta PVC"
ok "read-only, sin token de SA, con NetworkPolicy y PVC por defecto"

# El copilot.yaml rendido, cargado por el copiloto real con el entorno que el
# chart le daría.
"$PY" - "$tmp/minima.yaml" "$tmp/copilot.yaml" <<'PYEOF'
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "ConfigMap":
        open(sys.argv[2], "w").write(d["data"]["copilot.yaml"])
PYEOF
COPILOT_API_TOKEN=t PROMETHEUS_URL=http://p:9090 MODEL_BASE_URL=http://m/v1 MODEL_NAME=x \
COPILOT_STORAGE_PATH=/var/lib/copilot "$PY" - "$tmp/copilot.yaml" <<'PYEOF' || fail "el copilot.yaml rendido no carga"
import sys
from copilot.config import load
c = load(sys.argv[1])
assert not c.problems(), c.problems()
assert c.storage.path == "/var/lib/copilot" and c.metrics.options["url"] == "http://p:9090"
PYEOF
ok "el copilot.yaml del ConfigMap carga en el copiloto real"

# Sin MODEL_NAME el arranque corta nombrando la variable, no con un 500 después.
salida=$(COPILOT_API_TOKEN=t PROMETHEUS_URL=http://p:9090 MODEL_BASE_URL=http://m/v1 \
  COPILOT_CONFIG="$tmp/copilot.yaml" "$PY" -m copilot preflight 2>&1 || true)
echo "$salida" | grep -q "MODEL_NAME" || fail "una variable faltante no se nombra: $salida"
ok "una variable requerida que falta corta el arranque con su nombre"

echo; echo "Chart verificado."
