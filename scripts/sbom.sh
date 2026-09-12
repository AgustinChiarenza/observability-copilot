#!/usr/bin/env bash
# SBOM de la imagen, en CycloneDX. Es lo que el equipo de seguridad del cliente
# pide antes de dejar entrar un contenedor nuevo: qué hay adentro, con versión,
# para cruzarlo contra su base de CVEs. Se genera con syft (en su propia
# imagen, así no hay que instalar nada) y, si está grype, se escanea ahí mismo.
#
#   ./scripts/sbom.sh                       # construye la imagen genérica y la lista
#   EXTRAS=huawei ./scripts/sbom.sh         # con el SDK de Huawei adentro
#   IMAGE=ghcr.io/x/observability-copilot:0.1.0 ./scripts/sbom.sh   # una ya publicada
set -euo pipefail
cd "$(dirname "$0")/.."

EXTRAS="${EXTRAS:-}"
IMAGE="${IMAGE:-observability-copilot:local${EXTRAS:+-$EXTRAS}}"
OUT="${OUT:-dist}"
SYFT="${SYFT_IMAGE:-anchore/syft:v1.36.0}"
GRYPE="${GRYPE_IMAGE:-anchore/grype:v0.104.0}"
mkdir -p "$OUT"

if [ -z "${IMAGE_PREBUILT:-}" ] && [[ "$IMAGE" == observability-copilot:local* ]]; then
  echo "→ Construyendo $IMAGE ${EXTRAS:+(extras: $EXTRAS)}"
  docker build -q --build-arg "EXTRAS=$EXTRAS" -t "$IMAGE" . >/dev/null
fi

nombre="sbom${EXTRAS:+-$EXTRAS}"
echo "→ SBOM de $IMAGE"
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "$SYFT" \
  "docker:$IMAGE" -o "cyclonedx-json=/dev/stdout" -q > "$OUT/$nombre.cdx.json"
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "$SYFT" \
  "docker:$IMAGE" -o table -q > "$OUT/$nombre.txt"
n=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1])).get(\"components\", [])))" "$OUT/$nombre.cdx.json")
echo "  $OUT/$nombre.cdx.json  ($n componentes)"
echo "  $OUT/$nombre.txt"

if [ -z "${SKIP_SCAN:-}" ]; then
  echo "→ Vulnerabilidades conocidas (grype sobre el SBOM)"
  docker run --rm -v "$PWD/$OUT:/sbom:ro" "$GRYPE" "sbom:/sbom/$nombre.cdx.json" \
    --only-fixed -o table 2>/dev/null | tail -n +1 || echo "  (grype no disponible; el SBOM igual quedó)"
fi
