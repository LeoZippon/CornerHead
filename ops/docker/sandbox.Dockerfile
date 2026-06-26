# AutoTrade Agent sandbox image (docs/environment_design.md 3.1/3.3).
# Build: docker build -t autotrade-sandbox:latest -f ops/docker/sandbox.Dockerfile ops/docker
# Behind restricted networks pre-pull the base via a registry mirror and pass
# --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
FROM python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.org/simple

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        npm \
        ripgrep \
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

# Non-root agent user; Runner/root stays root for frozen execution and binds.
RUN useradd --create-home --uid 61000 agent

# Fixed mount points (populated by docker run -v).
RUN mkdir -p /mnt/snapshots /mnt/artifacts /mnt/agent /mnt/runtime && chown root:root /mnt

USER root
WORKDIR /mnt/agent
