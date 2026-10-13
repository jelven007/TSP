ARG USE_CN_MIRROR=1
ARG PYPI_INDEX=https://repo.huaweicloud.com/repository/pypi/simple
ARG PYPI_FALLBACK=https://mirrors.cloud.tencent.com/pypi/simple

FROM python:3.11-slim-bookworm AS builder
ARG UV_VERSION=0.12.17
ARG USE_CN_MIRROR
ARG PYPI_INDEX
ARG PYPI_FALLBACK
WORKDIR /build
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
      pip install --no-cache-dir "uv==${UV_VERSION}" -i "$PYPI_INDEX" || \
      pip install --no-cache-dir "uv==${UV_VERSION}" -i "$PYPI_FALLBACK"; \
    else \
      pip install --no-cache-dir "uv==${UV_VERSION}"; \
    fi
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv export --frozen --extra cell --no-dev --no-emit-project \
    --no-emit-package tsp-rustdx-native --format requirements-txt > /tmp/worker.txt
RUN python -m venv .venv \
    && if [ "$USE_CN_MIRROR" = "1" ]; then \
         UV_DEFAULT_INDEX="$PYPI_INDEX" UV_CONCURRENT_DOWNLOADS=1 \
           uv pip install --python .venv/bin/python --no-deps --require-hashes -r /tmp/worker.txt || \
         UV_DEFAULT_INDEX="$PYPI_FALLBACK" UV_CONCURRENT_DOWNLOADS=1 \
           uv pip install --python .venv/bin/python --no-deps --require-hashes -r /tmp/worker.txt; \
       else \
         uv pip install --python .venv/bin/python --no-deps --require-hashes -r /tmp/worker.txt; \
       fi
COPY backend/app ./app

FROM python:3.11-slim-bookworm
RUN groupadd --gid 10001 tsp && useradd --uid 10001 --gid tsp --no-create-home tsp
WORKDIR /app
COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/app ./app
ENV PATH=/app/.venv/bin:$PATH PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai \
    POLARS_MAX_THREADS=2 NUMBA_NUM_THREADS=2 NUMBA_CACHE_DIR=/tmp/tsp-numba \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 TICKFLOW_ENV_FILE=/dev/null
USER 10001:10001
CMD ["python", "-m", "app.cell.worker"]
