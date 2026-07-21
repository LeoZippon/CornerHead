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

# Complete, pinned research/DL stack. Data layer pins are the environment's
# byte-format contract (parquet caches) and stay put; the DL family follows
# "widely adopted, relatively recent": torch 2.10.0 is the last and most
# mature release of the CUDA 12.8 line, and the transformers/boosting/PyG
# picks are the current stable releases the ecosystem has settled on.
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} \
        pandas==2.2.3 \
        numpy==2.1.3 \
        pyarrow==18.1.0 \
        duckdb==1.1.3 \
        scipy==1.17.1 \
        scikit-learn==1.5.2 \
        statsmodels==0.14.4 \
        torch==2.10.0 \
        torchvision==0.25.0 \
        torch_geometric==2.8.0.post1 \
        transformers==5.14.1 \
        accelerate==1.14.0 \
        safetensors==0.8.0 \
        einops==0.8.2 \
        lightgbm==4.7.0 \
        xgboost==3.2.0 \
        ninja==1.13.0 \
        "huggingface_hub[cli]==1.24.0"

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

# CUDA build toolchain (nvcc + headers/dev libs), completing the pre-baked
# compiler policy above for CUDA-extension source builds (torch_scatter,
# torch_sparse, pyg_lib, ...) declared via sandbox_environment.json. Version
# matches the torch==2.10.0 wheel's CUDA 12.8. Installed from the sha256-pinned
# runfile (same pattern as the DuckDB CLI above) because the NVIDIA apt repo's
# SHA1-signed key is rejected by Debian trixie's Sequoia apt policy. The .cn
# CDN mirrors the canonical developer.download.nvidia.com bytes (md5 verified
# against the canonical md5sum.txt); the pin makes the mirror choice
# irrelevant. Nsight profilers are dropped to keep the layer lean. Placed
# after the pip/CLI layers so adding it kept their cache.
# TORCH_CUDA_ARCH_LIST targets the host L20s (sm_89): extension builds compile
# one arch instead of all, cutting derived-image build time several-fold.
ARG CUDA_RUNFILE_URL=https://developer.download.nvidia.cn/compute/cuda/12.8.1/local_installers/cuda_12.8.1_570.124.06_linux.run
# libxml2 is required by the runfile's cuda-installer (and by some CUDA tools
# at runtime); installed here rather than in the first apt layer so adding the
# toolchain did not invalidate the pip layer cache.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxml2 \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fL --retry 8 --retry-delay 3 --connect-timeout 30 --max-time 7200 \
        "${CUDA_RUNFILE_URL}" \
        -o /tmp/cuda.run \
    && echo "228f6bcaf5b7618d032939f431914fc92d0e5ed39ebe37098a24502f26a19797  /tmp/cuda.run" | sha256sum -c - \
    && sh /tmp/cuda.run --silent --toolkit --override --no-man-page \
    && rm -f /tmp/cuda.run \
    && rm -rf /usr/local/cuda-12.8/nsight* /usr/local/cuda-12.8/gds \
        /usr/local/cuda-12.8/libnvvp /usr/local/cuda-12.8/extras/demo_suite \
        /var/log/cuda-installer.log /tmp/cuda-installer.log \
    && /usr/local/cuda-12.8/bin/nvcc --version
# glibc 2.41 (Debian trixie) declares sinpi/cospi/sinpif/cospif with noexcept;
# CUDA 12.8's crt/math_functions.h re-declarations lack it, so any host-side
# nvcc compilation fails (NVIDIA fixed the headers in later toolkits). Mirror
# the distro-standard fix by annotating the four declarations; the count check
# fails the build if a toolkit bump changes the header shape.
RUN sed -i -E 's/^(extern __DEVICE_FUNCTIONS_DECL__ __device_builtin__ +(double|float) +(sinpif|cospif|sinpi|cospi)\(.*\));$/\1 noexcept (true);/' \
        /usr/local/cuda-12.8/include/crt/math_functions.h \
    && test "$(grep -c 'noexcept (true);' /usr/local/cuda-12.8/include/crt/math_functions.h)" = 4

# FORCE_CUDA: docker build has no GPU, so torch-extension setup scripts would
# silently produce CPU-only kernels; the flag makes every source build here
# AND in derived images compile real CUDA kernels for the arch below.
ENV CUDA_HOME=/usr/local/cuda-12.8 \
    PATH=/usr/local/cuda-12.8/bin:$PATH \
    TORCH_CUDA_ARCH_LIST=8.9 \
    FORCE_CUDA=1

# Compiled PyG companions, source-built against the baked torch + nvcc (no
# prebuilt cu128/2.10 wheels on PyPI). --no-build-isolation so the builds see
# the installed torch; ninja + the single-arch TORCH_CUDA_ARCH_LIST keep the
# compile bounded. The cuda_version() assertions enforce at build time that
# the extensions really contain CUDA kernels (importable-but-CPU-only would
# pass a plain import check).
RUN pip install --no-cache-dir --no-build-isolation -i ${PIP_INDEX_URL} \
        torch_scatter==2.1.2 \
        torch_sparse==0.6.18 \
        torch_cluster==1.6.3 \
    && python -c "import torch, torch_scatter, torch_sparse, torch_cluster, torch_geometric; \
assert torch.ops.torch_scatter.cuda_version() > 0, 'torch_scatter built without CUDA'; \
assert torch.ops.torch_sparse.cuda_version() > 0, 'torch_sparse built without CUDA'; \
assert torch.ops.torch_cluster.cuda_version() > 0, 'torch_cluster built without CUDA'"

# Trusted host-side runtime module baked in (R16): the de-stringed per-tick
# main(ctx) driver, loaded by file (executor.runtime_path -> CONTAINER_RUNTIME_DIR).
# It is standard-library only (broker actions are delayed-submit plans settled by the
# host, so the driver no longer needs broker_core). Must match
# autotrade.environment.executor.CONTAINER_RUNTIME_DIR.
RUN mkdir -p /opt/at_runtime
COPY src/autotrade/environment/replay/driver.py /opt/at_runtime/driver.py
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
