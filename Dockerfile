# Build en dos etapas: la imagen final no lleva compilador ni cabeceras de
# desarrollo. Va a correr en el cluster de un cliente, y todo lo que sobra es
# superficie que su escáner de vulnerabilidades le va a marcar a él, no a vos.
FROM python:3.12-slim AS build

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY copilot ./copilot
RUN pip install --no-cache-dir .

# --- runtime ---------------------------------------------------------------
FROM python:3.12-slim

# Usuario sin privilegios y sin shell. Muchos clusters tienen una PodSecurity
# que rechaza contenedores que corren como root, y descubrirlo en la
# instalación es perder la mañana.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin copilot

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
