FROM node:20-alpine AS frontend
WORKDIR /build
RUN npm install -g pnpm@9
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
ENV VITE_CELL_MODE=true
RUN pnpm build

FROM python:3.11-slim-bookworm AS builder
ARG UV_VERSION=0.12.17
WORKDIR /build
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
COPY backend/pyproject.toml backend/uv.lock ./
COPY deploy/cell/select_requirements.py /tmp/select_requirements.py
# Install only the independent API stack, with versions taken from the project lock.
# The Cell image contains no source-market driver, AI CLI, or legacy entrypoint.
RUN uv export --frozen --extra cell --no-dev --no-emit-project \
    --no-emit-package tsp-rustdx-native --format requirements-txt > /tmp/locked.txt \
    && python /tmp/select_requirements.py /tmp/locked.txt /tmp/api.txt
RUN python -m venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-deps --require-hashes -r /tmp/api.txt

FROM python:3.11-slim-bookworm
RUN groupadd --gid 10001 tsp && useradd --uid 10001 --gid tsp --no-create-home tsp
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY backend/app/__init__.py ./app/__init__.py
COPY backend/app/cell ./app/cell
COPY backend/app/strategy/__init__.py ./app/strategy/__init__.py
COPY backend/app/strategy/paper_math.py ./app/strategy/paper_math.py
COPY --from=frontend /build/dist ./static
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai TSP_CELL_STATIC_DIR=/app/static
USER 10001:10001
EXPOSE 3018
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3018/health/ready', timeout=4)"]
CMD ["uvicorn", "app.cell.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "3018", "--no-proxy-headers"]
