# syntax=docker/dockerfile:1.6
FROM python:3.11-slim-bookworm

# TA-Lib C library — install via official .deb release. The Python `TA-Lib`
# wrapper builds a C extension at pip-install time against these headers, so
# build-essential must be present during pip install (we keep it lean by
# purging dev tools after, but Python wrappers usually need them at install
# time only — TA-Lib is fine to leave installed since the wrapper is built
# in the same RUN).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        wget \
        ca-certificates \
    && wget -q https://github.com/TA-Lib/ta-lib/releases/download/v0.6.4/ta-lib_0.6.4_amd64.deb \
        -O /tmp/ta-lib.deb \
    && apt-get install -y --no-install-recommends /tmp/ta-lib.deb \
    && rm /tmp/ta-lib.deb \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for the runtime (security best practice — root inside
# the container is still namespaced but defense-in-depth)
RUN useradd --create-home --uid 1000 traderay
WORKDIR /app

# Layer cache: copy requirements first so pip layer is reused across code edits
COPY --chown=traderay:traderay requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy app code (respect .dockerignore)
COPY --chown=traderay:traderay . .

USER traderay

# Default: backend. `traderay-ui` service in docker-compose overrides with
# `command: streamlit run ...`
CMD ["python", "main.py"]
