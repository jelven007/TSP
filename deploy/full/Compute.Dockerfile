# The caller supplies the exact candidate runtime image, with frozen lock deps.
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
WORKDIR /app
COPY backend/app ./app
RUN .venv/bin/python -m app.plugins.rustdx.client
COPY tiers.yaml /app/tiers.yaml
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/python", "-m", "app.multitenancy.compute_worker"]
