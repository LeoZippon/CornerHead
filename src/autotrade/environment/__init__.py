"""Environment layer: PIT snapshots, Sandbox, tools, simulated Broker, LLM proxy.

Architecture boundaries: this package must not import from ``autotrade.agent``,
and must not import from ``autotrade.data_sources`` (the ingest adapter depends
on the environment's raw-lake contract in ``data/contracts.py``, never the
reverse).

Layout — subpackages are multi-module subsystems, top-level modules are
single-purpose components:

- ``data/``: PIT availability contracts (incl. the raw-lake research contract),
  raw-lake store, dataset transforms, snapshot builder, research-release
  pinning, agent data summary.
- ``replay/``: bar-level replay core (host engine, sandbox driver, market
  sources, result stats, rolling PIT timeview, state staging, post-replay
  style attribution).
- ``tools/``: Agent-facing tool contracts dispatched by the session runner.
- ``llm/``: host-side LLM provider boundary (keys never reach the sandbox).
- ``nl/``: NL Sub Agent stack (engine, PIT text retrieval, host RPC service).
- ``web/``: host-side web fetch/search services (meta-learning only).
- ``broker.py``/``broker_core.py``: simulated dual-account Broker and its pure
  fill/credit math.
- ``sandbox.py``/``sandbox_images.py``/``executor.py``/``gpu.py``: sandbox
  lifecycle, derived images, command executors, GPU selection.
- ``runtime.py``/``identity.py``/``artifacts.py``/``step_tree.py``:
  cross-cutting run primitives (paths/manifest/trace, agent-visible refs,
  artifact contracts, validated-step lineage).
- ``managed_proxy.py``: meta-learning egress proxy.
"""
