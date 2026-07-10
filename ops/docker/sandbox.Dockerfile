# AutoTrade Agent sandbox image (docs/environment_design.md 3.1/3.3).
# Build (context is the repo root so the trusted runtime modules can be copied in):
#   docker build -t autotrade-sandbox:latest -f ops/docker/sandbox.Dockerfile .
# Behind restricted networks pre-pull the base via a registry mirror, pass
# --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple, and add
# --network=host so the build's curl (DuckDB CLI release) uses the host network.
# The base is pinned by digest so the same Dockerfile always builds from the
# same bits (a floating tag can silently change between builds). To bump:
#   docker pull python:3.11-slim && docker image inspect python:3.11-slim --format '{{join .RepoDigests ","}}'
FROM python:3.11-slim@sha256:b27df5841f3355e9473f9a516d38a6783b6c8dfeacaf2d14a240f443b368ddb6

ARG PIP_INDEX_URL=https://pypi.org/simple

# Pre-bake the C/C++/Fortran build toolchain so a fold that pins a source-only
# wheel (e.g. torch_scatter/torch_sparse) builds without declaring apt_packages.
# Without this, the base python:3.11-slim has no compiler and such installs fail
# at build time (the root cause of an early GNN-transfer run's image-build error).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        npm \
        ripgrep \
        build-essential \
        g++ \
        gfortran \
        python3-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} \
        pandas==2.2.3 \
        numpy==2.1.3 \
        pyarrow==18.1.0 \
        duckdb==1.1.3 \
        scikit-learn==1.5.2 \
        statsmodels==0.14.4 \
        torch==2.5.1 \
        "huggingface_hub[cli]==0.27.1"

RUN if ! command -v hf >/dev/null 2>&1 && command -v huggingface-cli >/dev/null 2>&1; then \
        ln -s "$(command -v huggingface-cli)" /usr/local/bin/hf; \
    fi

# DuckDB CLI binary, pinned to the Python package version. The Agent's data-probe
# guidance uses `duckdb -c "..."`; without the CLI it fails with exit 127 and the
# Agent wastes turns falling back. (curl is already installed; release zip extracted
# with the bundled Python to avoid an extra apt dependency. Host is x86_64.)
RUN curl -fL --retry 8 --retry-all-errors --retry-delay 3 --connect-timeout 30 --max-time 600 \
        https://github.com/duckdb/duckdb/releases/download/v1.1.3/duckdb_cli-linux-amd64.zip \
        -o /tmp/duckdb_cli.zip \
    && echo "efd0fccdb1a28d9ec7a6ebfcde59900068b8ba43a846c9b553c0fd2bbe4acf43  /tmp/duckdb_cli.zip" | sha256sum -c - \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/duckdb_cli.zip').extractall('/usr/local/bin')" \
    && chmod +x /usr/local/bin/duckdb \
    && rm /tmp/duckdb_cli.zip \
    && duckdb -c "select 1"

# Trusted host-side runtime module baked in (R16): the de-stringed per-tick
# main(ctx) driver, loaded by file (executor.runtime_path -> CONTAINER_RUNTIME_DIR).
# It is standard-library only (broker actions are delayed-submit plans settled by the
# host, so the driver no longer needs broker_core). Must match
# autotrade.environment.executor.CONTAINER_RUNTIME_DIR.
RUN mkdir -p /opt/at_runtime
COPY src/autotrade/environment/main_ctx_driver.py /opt/at_runtime/main_ctx_driver.py
# COPY preserves the source mode (0600 on the host), so make the trusted module
# world-readable for the non-root `agent` user that runs the driver.
RUN chmod 0644 /opt/at_runtime/*.py

# Non-root agent user; Runner/root stays root for frozen execution and binds.
RUN useradd --create-home --uid 61000 agent

# Fixed mount points (populated by docker run -v).
RUN mkdir -p /mnt/snapshots /mnt/artifacts /mnt/agent /mnt/runtime && chown root:root /mnt

# Image default user stays root (the build never switches away); the executor
# selects the non-root agent user per-process at docker run time.
WORKDIR /mnt/agent
