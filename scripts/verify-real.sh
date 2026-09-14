#!/usr/bin/env bash
# La prueba en lo del cliente: cada puerto con la credencial real, sin modelo
# primero y con modelo al final. No imprime secretos: todo lo que sale pasa por
# un filtro que tapa los valores de las variables sensibles del .env.
#
#   scripts/verify-real.sh              # preflight + tools de lectura + detector en seco
#   scripts/verify-real.sh --notify     # además manda el mensaje de prueba por cada canal
#   scripts/verify-real.sh --ask "..."  # además una pregunta al modelo
#
# Necesita .env completo (ver .env.example) y config/copilot.yaml apuntando a
# ${VAR}. Los dos están gitignorados.
set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
CFG="${CFG:-config/copilot.yaml}"
NOTIFY=0; ASK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --notify) NOTIFY=1 ;;
    --ask) ASK="$2"; shift ;;
    *) echo "argumento desconocido: $1" >&2; exit 2 ;;
  esac
  shift
done

[ -f .env ] || { echo "falta .env (copiá .env.example y completalo)" >&2; exit 2; }
set -a; . ./.env; set +a
export COPILOT_CONFIG="$CFG"

# El filtro lee los valores del entorno, no de la línea de comandos: no
# aparecen en `ps` ni en este archivo.
redact() {
  "$PY" -c '
import os, sys
secretos = [v for k in ("HW_AK", "HW_SK", "MODEL_API_KEY", "COPILOT_API_TOKEN")
            if (v := os.environ.get(k, "")) and len(v) >= 6]
for linea in sys.stdin:
    for v in secretos:
        linea = linea.replace(v, "***")
    sys.stdout.write(linea)
'
}

OK=0; FAIL=0
paso() {   # paso "título" cmd...
  local titulo="$1"; shift
  echo; echo "== $titulo"
  if "$@" 2>&1 | redact; then OK=$((OK+1)); else FAIL=$((FAIL+1)); echo "   -> FALLÓ: $titulo"; fi
}

paso "preflight: cada puerto con la credencial real" "$PY" -m copilot preflight
paso "metric_metadata: qué métricas ve" "$PY" -m copilot tool metric_metadata '{"limit": 15}'
paso "alerts_active: qué alarmas están sonando" "$PY" -m copilot tool alerts_active
paso "alert_rules: qué reglas hay" "$PY" -m copilot tool alert_rules
paso "promql_instant: una consulta de métricas (VERIFY_QUERY en .env)" \
  "$PY" -m copilot tool promql_instant "{\"query\": \"${VERIFY_QUERY:-up}\"}"
paso "cost_daily: el gasto de los últimos días" "$PY" -m copilot tool cost_daily '{"days": 7}'
paso "cost_by_service: por servicio" "$PY" -m copilot tool cost_by_service '{"days": 7}'
paso "cost_spike en seco: evalúa sin despachar" "$PY" -m copilot detect cost_spike --dry-run
[ "$NOTIFY" = 1 ] && paso "notify-test: un mensaje por cada canal" "$PY" -m copilot notify-test
[ -n "$ASK" ]     && paso "ask: una pregunta con el modelo real" "$PY" -m copilot ask "$ASK"

echo; echo "== $OK pasos OK, $FAIL fallaron"
[ "$FAIL" = 0 ]
