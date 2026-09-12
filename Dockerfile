FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG TARGETARCH=amd64
ARG CODEX_VERSION=0.154.0
ARG APP_VERSION=0.2.0
ARG APP_COMMIT=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    CODEX_HOME=/app/codex-home \
    HEALTH_FILE=/tmp/codex-notify-health.json \
    APP_VERSION=${APP_VERSION} \
    APP_COMMIT=${APP_COMMIT}

WORKDIR /app

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    case "$TARGETARCH" in \
      amd64) codex_arch='x86_64';; \
      arm64) codex_arch='aarch64';; \
      *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1;; \
    esac; \
    archive="codex-package-${codex_arch}-unknown-linux-musl.tar.gz"; \
    release="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}"; \
    curl -fsSLO "$release/$archive"; \
    curl -fsSLO "$release/codex-package_SHA256SUMS"; \
    grep "  ${archive}$" codex-package_SHA256SUMS | sha256sum -c -; \
    mkdir /tmp/codex-release; \
    tar -xzf "$archive" -C /tmp/codex-release; \
    codex_binary="$(find /tmp/codex-release -type f -name codex | head -n 1)"; \
    test -n "$codex_binary"; \
    install -m 0755 "$codex_binary" /usr/local/bin/codex; \
    codex --version | grep -F "$CODEX_VERSION"; \
    rm -rf /tmp/codex-release "$archive" codex-package_SHA256SUMS; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-deps --require-hashes -r /app/requirements.txt

COPY pyproject.toml README.md /app/
COPY codex_notify /app/codex_notify

RUN groupadd --gid 10001 codex-notify \
    && useradd --uid 10001 --gid codex-notify --no-create-home --home-dir /app codex-notify \
    && mkdir -p /app/data /app/codex-home \
    && chown -R codex-notify:codex-notify /app/data /app/codex-home

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "codex_notify.healthcheck"]

CMD ["python", "-m", "codex_notify"]
