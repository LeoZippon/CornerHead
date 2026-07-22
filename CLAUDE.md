# Repository Guidelines

## Environment

- Python Environment is `quant` installed at `~/miniconda3/envs/quant` with Python 3.11.
- Docker sandbox Python is independent from the local conda environment; rebuild `ops/docker/sandbox.Dockerfile` when sandbox dependencies change.
- Before editing or running commands that write files, confirm the real path with `pwd -P` or `realpath`.

## Repository Organization

- Keep production implementation under `src/autotrade/`; `scripts/` should contain thin command-line entrypoints only.
- Group scripts by responsibility instead of leaving unrelated entrypoints at the top level: data operations in `scripts/data/`, experiment runs and reports in `scripts/experiments/`, and developer utilities in `scripts/dev/`.
- Avoid adding new one-off top-level scripts. If a script grows substantial business logic, move the logic into `src/autotrade/` and keep the script as a small wrapper.

## Git and GitHub

- The canonical remote repository is already configured; prefer SSH Git operations and keep `origin` aligned with it.
- Use branch prefixes by change type: `fix/` for bug or data-integrity fixes, `feat/` for new capabilities, `docs/` for documentation-only updates, `refactor/` for internal restructuring, `test/` for tests, `ops/` for deployment/scheduling changes, and `chore/` for maintenance.
- Keep commits focused and self-contained. Code, tests, and living documentation for the same behavior change should usually be committed together.
- Use concise imperative commit subjects; add a short body when validation commands or operational impact matter.
- PR titles, descriptions, review comments, and discussion comments may be written in Chinese when that is clearer for project collaboration.
- Prefer concise English imperative commit subjects for tooling/search consistency. Chinese commit subjects are acceptable for human-facing milestones or domain-specific wording; commit bodies may use Chinese for context and validation details.
- Before committing, remove generated caches such as `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `*.pyc`, and `*.pyo`; never commit runtime logs, local state, data dumps, API keys, scratch notebooks, or ignored artifacts.
- Run `git status` before and after changes, review `git diff --cached` before committing, and leave unrelated local changes unstaged.
- Before each commit or PR, run the smallest meaningful verification set plus `git diff --check`, and record important results in `LOGBOOK.md` and `docs/logbook/DETAILED_LOGBOOK.md`.
- Pull and rebase or merge carefully before pushing when the remote branch has moved. Do not rewrite shared history, force-push, or use destructive Git commands unless explicitly approved.

## Mutagen
- @data/, @results/, @wandb/ is ignored from local repository, but you can check and read using terminal commands.

## Logging

- Record logs promptly for every training, inference, evaluation, or data-processing run.
- Use `@LOGBOOK.md` as the concise current logbook: keep entries short and focused on what was tried, the key result, and the current conclusion.
- Use `@docs/logbook/DETAILED_LOGBOOK.md` as the detailed durable logbook: include the date, task, key command or config, resource checks, important artifact/log paths, and final result or conclusion.
- For routine context gathering, read `@LOGBOOK.md` first. Read `@docs/logbook/DETAILED_LOGBOOK.md` only when detailed historical commands, paths, or experiment context are needed.
- Runtime log files under `logs/` may be kept locally for debugging, but they must not be committed to Git.

## Living Documentation

- Treat the current design docs as the communication layer between audit, research decisions, and implementation.
- Keep five authoritative living docs aligned by scope:
  - `@docs/data_documentation.md`: data sources, downloads, audits, PIT availability rules, unit rules, and known data risks.
  - `@docs/agent_design.md`: Agent-visible inputs, writable strategy artifacts, prompt protocol, tool usage semantics, and forbidden behavior.
  - `@docs/environment_design.md`: PIT snapshots, Sandbox/runtime paths, trusted tools, Broker/backtest/NL scoring, LLM API boundary, and run logs.
  - `@docs/pipeline_design.md`: Fold/Epoch/Held-out orchestration, artifact handoff, freeze/fallback rules, ledgers, and reporting.
  - `@docs/deployment_documentation.md`: deployment surfaces — the CornerHead console's three-machine network architecture, frontend service, tunnels/keepalive/startup workflow, plus the QMT in-client Python API, file-bridge architecture, and live-operation workflow.
- Keep `@docs/parameters_reference.md` as a derived quick reference, not a sixth authoritative design doc. When defaults, CLI/config knobs, Broker profile fields, replay budgets, sandbox/tool limits, data-task limits, or QMT constants change, update the relevant authoritative doc and this parameter reference in the same work item. Code and run/snapshot manifests remain the source of truth for actually effective values.
- When a change materially affects one of these areas, update the relevant document in the same work item. Do not rely on code or logs alone to communicate a changed design, data contract, operating procedure, or parameter default.
- Keep these documents concise and current. They should describe the latest accepted state, not a chronology of earlier attempts, old names, or superseded workflows.
- Detailed historical traces belong in `@LOGBOOK.md` and `@docs/logbook/DETAILED_LOGBOOK.md`; do not preserve obsolete version labels or migration notes in the living design docs unless they are still operationally relevant.
- Add a new durable document under `docs/` when a new project area becomes important enough that the existing documents would become confusing or overloaded.

## Memory and GPU Monitoring

- Check GPU memory and system memory before and after experiment, training, inference, evaluation, or data-processing runs.
- Routine read-only inspection, small documentation edits, prompt export, formatting checks, and targeted lightweight unit tests do not require RAM/GPU checks unless they are expected to be resource-intensive.
- Stop and adjust the workload if memory usage becomes unsafe.

Recommended checks:

```bash
nvidia-smi
free -h
```

## Execution Workflow

For experiment, training, inference, evaluation, or data-processing jobs:

1. Verify free system RAM and GPU memory.
2. Start the job with logging enabled.
3. Recheck memory usage after the job starts and after it finishes.
4. Write the detailed traceability record to `@docs/logbook/DETAILED_LOGBOOK.md`, write the concise result to `@LOGBOOK.md`, and keep `logs/` out of commits.

## Development Principles

- Maintain a minimalist code architecture and implementation while ensuring logical correctness and completeness.
- Achieve optimal performance while keeping the environment as close to real-world conditions as possible.
- Maximize the Agent’s autonomy while lowering the complexity of interactions between the Agent and the environment.
- Freeze the scope and define contracts and invariants first; require reproducible evidence of material impact, and distinguish defects from suggestions and accepted limitations.
- Fix one root cause per small, self-contained change and leave overall code health better; redesign instead of stacking exceptions when complexity keeps growing.
- Prefer explicit failure over silent fallback or false success when correctness cannot be guaranteed.
- Test invariants, negative paths, and realistic end-to-end behavior rather than only the current implementation's happy path.
- Record irreducible limitations honestly; do not add speculative abstractions, compatibility branches, or unsupported recovery behavior.

## Operational Guardrails

- Fully read sufficient code and supporting documentation to form a sound design idea before writing or modifying any code.
- Treat resource checks and logging as mandatory steps, not optional cleanup.
- Keep the repository organized, clean and tidy.
- When starting a sub-agent, always choose the best performing ones.
- Include the three repository design principles above explicitly in every sub-agent's task prompt.
- Dare to break thinking inertia and rethink when necessary.
