#!/usr/bin/env bash
# Verificación del criterio de salida de F0 contra contenedores de verdad.
#
#   «docker run + un Prometheus de juguete → /v1/chat contesta cuántos targets
#    están up.»
#
# Lo que prueba este script y no prueban los tests: la red real, la imagen real
# y un Prometheus real —con su API v1, su parseo y sus targets de verdad, uno de
# ellos caído a propósito—.
#
# El último paso (el chat) necesita un endpoint de modelo. Si no hay
# MODEL_BASE_URL en el entorno, el script lo dice y saltea SOLO ese paso: todo
# lo demás igual se verifica.
set -euo pipefail

cd "$(dirname "$0")/.."
TOKEN="${COPILOT_API_TOKEN:-dev-token-no-usar-en-produccion}"
export COPILOT_API_TOKEN="$TOKEN"

# Puertos altos por defecto: en una máquina de desarrollo casi siempre hay otro
# stack con algo en 9090, y esta verificación no tiene por qué pelearse con él.
export COPILOT_HOST_PORT="${COPILOT_HOST_PORT:-18080}"
export PROM_HOST_PORT="${PROM_HOST_PORT:-19090}"
APP="http://localhost:${COPILOT_HOST_PORT}"
PROM="http://localhost:${PROM_HOST_PORT}"
echo "copiloto en ${APP} · prometheus en ${PROM}"

ok()   { printf '  \033[32m[OK]\033[0m    %s\n' "$1"; }
fail() { printf '  \033[31m[FALLA]\033[0m %s\n' "$1"; exit 1; }
skip() { printf '  \033[33m[SALTEA]\033[0m %s\n' "$1"; }

cleanup() { docker compose down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "→ Levantando el stack"
docker compose up -d --build >/dev/null

echo "→ Esperando a que el copiloto responda"
for _ in $(seq 1 60); do
  curl -sf ${APP}/healthz >/dev/null 2>&1 && break
  sleep 1
done
curl -sf ${APP}/healthz >/dev/null || fail "el copiloto no levantó"
ok "/healthz responde"

echo "→ Esperando el PRIMER SCRAPE (no alcanza con que el target exista)"
# El `|| true` no es decorativo: con `set -euo pipefail`, un grep que no matchea
# devuelve 1 y mata el script entero justo mientras Prometheus todavía no
# scrapeó nada — que es exactamente el caso que este loop está esperando.
for _ in $(seq 1 40); do
  # Contar `"health"` no sirve: antes del primer scrape el target ya figura,
  # con health "unknown". Hay que esperar a los que están realmente "up".
  n=$(curl -sf "${PROM}/api/v1/targets?state=active" 2>/dev/null \
      | grep -o '"health":"up"' | wc -l | tr -d ' ' || true)
  [ "${n:-0}" -ge 2 ] && break
  sleep 1
done

echo
echo "→ Preflight (toca cada puerto configurado)"
docker compose exec -T copilot python -m copilot preflight || true

echo
echo "→ Verificaciones"

# Sin -f a propósito: sin modelo configurado /readyz devuelve 503 con razón —el
# producto NO está listo para contestar— y aun así el cuerpo dice, puerto por
# puerto, qué anda y qué no. Eso es justo lo que hay que verificar acá.
readyz=$(curl -s "${APP}/readyz")
echo "$readyz" | grep -q '"metrics": *"ok"' || fail "/readyz no ve el Prometheus: $readyz"
ok "/readyz alcanza el Prometheus real"

if [ -n "${MODEL_BASE_URL:-}" ]; then
  echo "$readyz" | grep -q '"ready": *true' || fail "con modelo configurado debería estar listo"
  ok "/readyz reporta listo"
else
  echo "$readyz" | grep -q '"ready": *false' \
    || fail "sin modelo NO debería reportarse listo"
  ok "/readyz reporta no-listo sin modelo (correcto: no puede contestar)"
fi

curl -sf ${APP}/metrics | grep -q copilot_http_requests_total \
  || fail "/metrics no expone las métricas del copiloto"
ok "/metrics sirve formato Prometheus"

# Otra vez sin -f: acá el 401 ES el resultado esperado, y con -f curl no
# imprimiría el código sino que fallaría.
codigo=$(curl -s -o /dev/null -w '%{http_code}' "${APP}/v1/status")
[ "$codigo" = "401" ] || fail "/v1/status contestó $codigo sin token, se esperaba 401"
ok "las rutas de datos exigen token"

curl -sf -H "Authorization: Bearer $TOKEN" ${APP}/v1/status \
  | grep -q '"prometheus"' || fail "/v1/status no reporta el adapter"
ok "con token, /v1/status reporta los puertos"

# La cadena entera hasta los datos, sin modelo: adapter real → API v1 real.
salida=$(docker compose exec -T copilot python -m copilot tool targets_health '{}')
echo "$salida" | grep -q '"total": 3' || fail "no vio los 3 targets: $salida"
echo "$salida" | grep -q '"up": 2'    || fail "no contó 2 arriba: $salida"
echo "$salida" | grep -q caido_a_proposito || fail "no identificó el target caído"
ok "targets_health lee el Prometheus real: 2 de 3 arriba, el caído identificado"

salida=$(docker compose exec -T copilot python -m copilot tool \
  promql_instant '{"query":"up"}')
echo "$salida" | grep -q '"count": 3' || fail "promql_instant no devolvió 3 series"
ok "promql_instant corre PromQL contra el Prometheus real"

# El presupuesto, contra el backend de verdad.
salida=$(docker compose exec -T copilot python -m copilot tool \
  promql_instant '{"query":"{}"}' 2>&1 || true)
echo "$salida" | grep -qi "selector vacío" || fail "el selector vacío no se rechazó"
ok "un selector sin matchers se rechaza antes de salir a la red"

echo
if [ -n "${MODEL_BASE_URL:-}" ]; then
  echo "→ El turno completo, con modelo"
  respuesta=$(curl -sf -X POST ${APP}/v1/chat \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"message":"¿cuántos targets están up? nombrá el que esté caído"}')
  echo "$respuesta" | python3 -m json.tool
  echo "$respuesta" | grep -q '"tool": *"targets_health"' \
    || fail "el modelo no usó la tool"
  ok "/v1/chat contestó usando la tool, con la traza en la respuesta"
else
  skip "/v1/chat — definí MODEL_BASE_URL, MODEL_API_KEY y MODEL_NAME para probarlo"
  echo "       (todo lo anterior a la llamada al modelo ya quedó verificado)"
fi

echo
echo "Criterio de salida de F0: verificado."
