"""Bar-level replay core: the host engine, the sandbox-side driver, and their
supporting market/result/PIT-view/state modules.

- ``engine``: host-side replay orchestrator (``run_main_ctx_replay``,
  ``MainPolicyRunner``) driving the fixed exchange-minute clock and the Broker.
- ``driver``: sandbox-side, standard-library-only ``main(ctx)`` driver baked
  into the sandbox image; launched by file, never imported by host code.
- ``market``: optional minute-market data sources for the replay clock.
- ``stats``: ``ReplayResult`` container and return-statistics reducer.
- ``timeview``: per-tick rolling as-of PIT view over snapshot + replay parts.
- ``state_staging``: latency-modeled staging of ``ctx.state_dir`` writes.
- ``style``: Barra-lite benchmark/style attribution over frozen replay outputs.

Modules are imported directly (no re-exports): the engine stack is heavy and
the driver must stay import-free.
"""
