# The caller supplies the exact candidate runtime image, with frozen lock deps.
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
WORKDIR /app
RUN python /app/build_manifest.py verify --root /app --manifest /app/build-manifest.json
# Remove build-layer source so deleted modules cannot survive COPY directory merges.
RUN rm -rf /app/app
COPY backend/app ./app
RUN .venv/bin/python -m app.plugins.rustdx.client
COPY tiers.yaml /app/tiers.yaml
COPY VERSION /app/VERSION
COPY scripts/build_manifest.py /app/build_manifest.py
ARG SOURCE_COMMIT=
RUN python /app/build_manifest.py create --root /app \
    --include app --include static --include tiers.yaml --include VERSION \
    --include pyproject.toml --include uv.lock --include native-wheels \
    --include build_manifest.py --version-file /app/VERSION \
    --commit "$SOURCE_COMMIT" --output /app/compute-build-manifest.json \
    && mv /app/compute-build-manifest.json /app/build-manifest.json \
    && python /app/build_manifest.py verify --root /app --manifest /app/build-manifest.json
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/python", "-m", "app.multitenancy.compute_worker"]
