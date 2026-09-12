# Build en dos etapas: la imagen final no lleva compilador ni cabeceras de
# desarrollo. Va a correr en el cluster de un cliente, y todo lo que sobra es
# superficie que su escáner de vulnerabilidades le va a marcar a él, no a vos.
FROM python:3.14-slim AS build

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY copilot ./copilot
# EXTRAS elige qué SDKs de proveedor entran en la imagen. Vacío = ninguno: la
# imagen genérica no lleva el SDK de Huawei, y la de un cliente Huawei se
# construye con --build-arg EXTRAS=huawei.
ARG EXTRAS=""
# pip no viaja a la imagen final: nada se instala en runtime, y un pip viejo
# es una línea más en el escaneo de vulnerabilidades del cliente.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".${EXTRAS:+[$EXTRAS]}" \
    && pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true

# --- runtime ---------------------------------------------------------------
FROM python:3.14-slim

# El pip del sistema tampoco hace falta, y el escáner lo ve igual.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip* \
           /usr/local/lib/python3.14/site-packages/setuptools* \
           /usr/local/bin/pip*

# Usuario sin privilegios y sin shell. Muchos clusters tienen una PodSecurity
# que rechaza contenedores que corren como root, y descubrirlo en la
# instalación es perder la mañana.
RUN groupadd --system --gid 10001 copilot \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin copilot \
    # El directorio de datos existe en la imagen y es del usuario: un volumen
    # que se monte ahí hereda ese dueño, en vez de aparecer como root y dejar
    # al proceso sin poder escribir su propia auditoría.
    && mkdir -p /var/lib/copilot && chown 10001:10001 /var/lib/copilot

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COPILOT_CONFIG=/etc/copilot/copilot.yaml

USER 10001
EXPOSE 8080

# La liveness sale contra /healthz, que no toca ninguna dependencia: si
# respondiera por el Prometheus del cliente, el orquestador reiniciaría el pod
# justo cuando ese Prometheus está lento — es decir, cuando más falta hace.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "copilot"]
CMD ["serve"]
