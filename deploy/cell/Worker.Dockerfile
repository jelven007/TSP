FROM python:3.11-slim-bookworm AS builder
ARG UV_VERSION=0.12.17
WORKDIR /build
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/native ./native
RUN uv sync --frozen --no-dev --extra cell --no-install-project
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
