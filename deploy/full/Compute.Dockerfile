# The caller supplies the exact candidate runtime image, with frozen lock deps.
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
WORKDIR /app
RUN python /app/build_manifest.py verify --root /app --manifest /app/build-manifest.json
# The full API exposes signal/factor backtests through the compute protocol.
# Keep packages already selected by the runtime image (for example legacy-cpu)
# while adding the frozen vectorbt closure required by signal_backtest.
ARG PYPI_LOCK_MIRROR=https://repo.huaweicloud.com/repository/pypi/simple
RUN uv export --quiet --frozen --no-dev --extra backtest \
    --no-emit-package tsp-rustdx-native --no-emit-project \
    --output-file /tmp/compute-locked.txt \
    && UV_EXTRA_INDEX_URL= UV_INDEX= UV_DEFAULT_INDEX="$PYPI_LOCK_MIRROR" \
       UV_HTTP_TIMEOUT=60 UV_HTTP_RETRIES=2 \
       uv pip install --python /app/.venv/bin/python --require-hashes \
       --requirements /tmp/compute-locked.txt \
    && rm /tmp/compute-locked.txt \
    && .venv/bin/python -c "import vectorbt"
# COPY merges directories; remove the image-layer copy so deleted source modules
# cannot survive under a new commit identity. This never touches a running volume.
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
