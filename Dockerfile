# 两阶段构建:前端 dist 拷进后端镜像,单容器运行
# 可选:构建网络无法直连官方源时,传入 --build-arg USE_CN_MIRROR=1 启用国内镜像
ARG BASE_IMAGE_REGISTRY=docker.io
ARG USE_CN_MIRROR=1
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
# 备用 PyPI 源:主源同步延迟/故障时自动兜底(阿里云与清华互为补充)
ARG PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple
ARG BACKEND_EXTRAS=
ARG CODEX_CLI_VERSION=0.144.3
ARG UV_VERSION=0.12.17

# === Stage 1: 前端构建 ===
FROM ${BASE_IMAGE_REGISTRY}/library/node:20-alpine AS frontend-builder
ARG USE_CN_MIRROR=1
ARG NPM_REGISTRY=https://registry.npmmirror.com
WORKDIR /build
# 关键:corepack 不读 npm 的 registry 配置,且跨 RUN 不保留环境变量,
# 因此国内网络下最稳的做法是直接用 npm 安装 pnpm(npm 会读取 .npmrc 镜像源),
# 彻底绕开 corepack 再次联网下载 pnpm 的问题。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then npm config set registry "$NPM_REGISTRY"; fi && \
    npm install -g pnpm@9
# 让 pnpm 走镜像源安装依赖
RUN if [ "$USE_CN_MIRROR" = "1" ]; then pnpm config set registry "$NPM_REGISTRY"; fi
COPY frontend/package.json frontend/pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# === Stage 1b: Codex CLI ===
# 固定版本保证镜像可复现；只复制安装产物到运行镜像，不保留 npm。
FROM ${BASE_IMAGE_REGISTRY}/library/node:20-bookworm-slim AS codex-builder
ARG USE_CN_MIRROR=1
ARG NPM_REGISTRY=https://registry.npmmirror.com
# 版本由顶层 ARG CODEX_CLI_VERSION 提供, 这里仅声明以继承, 不再重复默认值。
ARG CODEX_CLI_VERSION
RUN if [ "$USE_CN_MIRROR" = "1" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm install --global --prefix /opt/codex "@openai/codex@${CODEX_CLI_VERSION}" \
    && CODEX_NATIVE="$(find /opt/codex -type f -path '*/vendor/*/bin/codex' -print -quit)" \
    && test -n "$CODEX_NATIVE" \
    && cp "$CODEX_NATIVE" /opt/codex-native \
    && chmod +x /opt/codex-native \
    && /opt/codex-native --version

# === Stage 1c: rustdx native bridge ===
# Build once outside the runtime image. The resulting CPython 3.11 wheel is
# architecture-specific, so Buildx produces it independently for amd64/arm64.
FROM ${BASE_IMAGE_REGISTRY}/library/rust:1.99-slim-bookworm AS rustdx-builder
ARG USE_CN_MIRROR=1
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends git python3-dev python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /maturin \
    && if [ "$USE_CN_MIRROR" = "1" ]; then \
         /maturin/bin/pip install --no-cache-dir -i "$PYPI_INDEX" "maturin==1.9.6"; \
       else \
         /maturin/bin/pip install --no-cache-dir "maturin==1.9.6"; \
       fi \
    && if [ "$USE_CN_MIRROR" = "1" ]; then \
         mkdir -p "$CARGO_HOME"; \
         printf '%s\n' \
           '[source.crates-io]' \
           'replace-with = "rsproxy-sparse"' \
           '[source.rsproxy-sparse]' \
           'registry = "sparse+https://rsproxy.cn/index/"' \
           > "$CARGO_HOME/config.toml"; \
       fi
WORKDIR /build
COPY backend/native/rustdx_native ./
RUN --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/usr/local/cargo/registry \
    CARGO_NET_GIT_FETCH_WITH_CLI=true \
    /maturin/bin/maturin build --locked --release --interpreter python3 --out /wheels

# === Stage 2: Python 运行时 ===
FROM ${BASE_IMAGE_REGISTRY}/library/python:3.11-slim-bookworm AS runtime
ARG USE_CN_MIRROR=1
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple
ARG BACKEND_EXTRAS=
ARG UV_VERSION
WORKDIR /app

# Codex CLI 从官方 npm 包提取原生二进制，不依赖运行时 Node.js。
# 国内构建换 apt 阿里云镜像 (bookworm deb822 格式)。官方 python slim 镜像的
# sources 指向 deb.debian.org, 部分国内网络下 apt 极慢(实测阿里云 ECS 拉官方源
# 单阶段可达 8 分钟以上); USE_CN_MIRROR 与 npm/pypi 的换源开关注一脉相承。
# tesseract-ocr: 自选截图导入。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && tesseract --version

# 安装固定版本 uv。镜像源仅作下载链路兜底, 不改变依赖版本。
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
      pip install --no-cache-dir "uv==$UV_VERSION" -i "$PYPI_INDEX" || \
      pip install --no-cache-dir "uv==$UV_VERSION" -i "$PYPI_FALLBACK" || \
      pip install --no-cache-dir "uv==$UV_VERSION"; \
    else \
      pip install --no-cache-dir "uv==$UV_VERSION"; \
    fi

# Backend deps
COPY README.md /README.md
COPY backend/pyproject.toml backend/uv.lock* ./
# uv.lock contains the default local rustdx path source. Keep the source path
# present during frozen resolution even though the runtime wheel is installed
# separately below.
COPY backend/native ./native
# Frozen sync follows the artifact URLs in uv.lock, even with an index override.
# Preinstall the same hash-pinned closure through a mirror, then let frozen sync
# verify the environment and install the local project without changing the lock.
ARG PYPI_LOCK_MIRROR=https://repo.huaweicloud.com/repository/pypi/simple
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
      export UV_DEFAULT_INDEX="$PYPI_INDEX" UV_EXTRA_INDEX_URL="$PYPI_FALLBACK"; \
      set -- --no-dev --no-emit-package tsp-rustdx-native --no-emit-project; \
      for extra in $BACKEND_EXTRAS; do \
        set -- "$@" --extra "$extra"; \
      done; \
      uv export --quiet --frozen "$@" --output-file /tmp/runtime-locked.txt \
      && uv venv \
      && UV_EXTRA_INDEX_URL= UV_INDEX= UV_DEFAULT_INDEX="$PYPI_LOCK_MIRROR" \
         UV_HTTP_TIMEOUT=60 UV_HTTP_RETRIES=2 \
         uv pip sync --python /app/.venv/bin/python --require-hashes /tmp/runtime-locked.txt \
      && rm /tmp/runtime-locked.txt \
      || exit 1; \
    fi; \
    set -- --no-dev --no-install-package tsp-rustdx-native; \
    for extra in $BACKEND_EXTRAS; do \
      set -- "$@" --extra "$extra"; \
    done; \
    uv sync --frozen "$@"

# Install the default rustdx route's wheel built for this target architecture.
COPY --from=rustdx-builder /wheels /app/native-wheels
RUN uv pip install --python /app/.venv/bin/python /app/native-wheels/*.whl

# Backend code
# 注意:Docker 里 WORKDIR=/app, 而 config.py 的 _PROJECT_ROOT 是按开发布局
# (<root>/backend/app/) 推导的, 容器内会错算到 /。这里用环境变量显式指定
# 三个关键路径, 确保 static / tiers / data 都指向容器内正确位置。
COPY backend/app ./app
RUN .venv/bin/python -m app.plugins.rustdx.client
COPY tiers.yaml /app/tiers.yaml
ENV STATIC_DIR=/app/static \
    TIERS_YAML=/app/tiers.yaml \
    DATA_DIR=/app/data \
    TICKFLOW_ENV_FILE=/app/.env

# Frontend 静态产物
COPY --from=frontend-builder /build/dist ./static

# Codex CLI 使用官方 npm 包携带的当前平台原生二进制，无需运行时 Node.js。
COPY --from=codex-builder /opt/codex-native /usr/local/bin/codex
RUN codex --version

# Hash project runtime files and the exact native wheel; dependency closure remains
# governed by uv.lock. An omitted commit is explicitly unknown for local builds.
ARG SOURCE_COMMIT=
COPY VERSION /app/VERSION
COPY scripts/build_manifest.py /app/build_manifest.py
RUN python /app/build_manifest.py create --root /app \
    --include app --include static --include tiers.yaml --include VERSION \
    --include pyproject.toml --include uv.lock --include native-wheels \
    --include build_manifest.py --version-file /app/VERSION \
    --commit "$SOURCE_COMMIT" --output /app/build-manifest.json \
    && python /app/build_manifest.py verify --root /app --manifest /app/build-manifest.json

ENV PYTHONPATH=/app
# 运行时 uv 镜像源持久化: CMD 用 `uv run` 启动, 锁与 pyproject 不一致等场景下
# uv 会在容器内重新解析/安装 —— 无源配置时默认 pypi.org, 国内网络会卡死启动
# (实测阿里云 ECS)。与构建期 RUN 内的 export 同源, 这里让它跨层存活。
ARG PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple
ENV UV_DEFAULT_INDEX=${PYPI_INDEX} \
    UV_EXTRA_INDEX_URL=${PYPI_FALLBACK}
# 兜底时区: 交易时段判断已在代码里显式用北京时间 (app/market_time.py),
# 此处让日志时间戳等其余 naive 时间也对齐北京时间。
ENV TZ=Asia/Shanghai
EXPOSE 3018
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3018/health/ready', timeout=4)"]
CMD ["uv", "run", "--frozen", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3018"]
