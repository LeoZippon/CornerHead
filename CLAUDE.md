# Repository Guidelines

## Environment

- Python Environment is `quant` installed at `~/miniconda3/envs/quant` with Python 3.11.
- Use that installation for environment activation and package management.
- The previous `stock` environment may still exist for historical runs, but new local scripts, tests, cron jobs, and non-Docker tools should use `quant`.
- Docker sandbox Python is independent from the local conda environment; rebuild `ops/docker/sandbox.Dockerfile` when sandbox dependencies change.
- Prefer explicit environment setup over ad hoc local Python changes.
- The real writable repository root on this machine is `/Data/lzp/MacroQuant`.
- Paths under `/home/coder/projects/...` may be wrapper artifacts and should not be trusted for writes.
- Before editing or running commands that write files, confirm the real path with `pwd -P` or `realpath`.

## Repository Organization

- Keep production implementation under `src/hl_trader/`; `scripts/` should contain thin command-line entrypoints only.
- Group scripts by responsibility instead of leaving unrelated entrypoints at the top level: data operations in `scripts/data/`, experiment runs and reports in `scripts/experiments/`, and developer utilities in `scripts/dev/`.
- Avoid adding new one-off top-level scripts. If a script grows substantial business logic, move the logic into `src/hl_trader/` and keep the script as a small wrapper.

## Git and GitHub

- The canonical remote repository is already configured; prefer SSH Git operations and keep `origin` aligned with it.
- Work on reviewable branches and pull requests for non-trivial changes. Do not push directly to shared `main` unless explicitly requested.
- Use branch prefixes by change type: `fix/` for bug or data-integrity fixes, `feat/` for new capabilities, `docs/` for documentation-only updates, `refactor/` for internal restructuring, `test/` for tests, `ops/` for deployment/scheduling changes, and `chore/` for maintenance.
- Keep commits focused and self-contained. Code, tests, and living documentation for the same behavior change should usually be committed together.
- For broad work, split commits by the smallest coherent review and revert unit.
- Split work into multiple PRs when changes can be reviewed, tested, deployed, or reverted independently, such as data-ingestion fixes, Agent logic, Environment logic, ops/cron changes, and documentation-only updates.
- Keep one PR when the changes are tightly coupled and must land together to keep the repository runnable. Small follow-up docs/log updates may stay in the current PR when they do not distract from the main review.
- Do not mix unrelated changes in one PR solely because they were done in the same session.
- Use concise imperative commit subjects, for example `Harden TuShare cron ingestion`; add a short body when validation commands or operational impact matter.
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
- Record every real LLM provider API call to the local conversation-log JSONL contract for audit and possible future distillation. Logs must include prompts/messages and raw provider responses, but must never include API keys or Authorization headers.
- Use `@LOGBOOK.md` as the concise current logbook: keep entries short and focused on what was tried, the key result, and the current conclusion.
- Use `@docs/logbook/DETAILED_LOGBOOK.md` as the detailed durable logbook: include the date, task, key command or config, resource checks, important artifact/log paths, and final result or conclusion.
- For routine context gathering, read `@LOGBOOK.md` first. Read `@docs/logbook/DETAILED_LOGBOOK.md` only when detailed historical commands, paths, or experiment context are needed.
- Runtime log files under `logs/` may be kept locally for debugging, but they must not be committed to Git.
- If a script supports file logging, you may save logs to disk locally, but durable summaries in Git must still be written back to both the concise and detailed logbooks at the appropriate granularity.

## Living Documentation

- Treat the current design docs as the communication layer between audit, research decisions, and implementation.
- Keep exactly five current living docs aligned by scope:
  - `@docs/data_documentation.md`: data sources, downloads, audits, PIT availability rules, unit rules, and known data risks.
  - `@docs/agent_design.md`: Agent-visible inputs, writable strategy artifacts, prompt protocol, tool usage semantics, and forbidden behavior.
  - `@docs/environment_design.md`: PIT snapshots, Sandbox/runtime paths, trusted tools, Broker/backtest/NL scoring, LLM API boundary, and run logs.
  - `@docs/pipeline_design.md`: Fold/Epoch/Held-out orchestration, artifact handoff, freeze/fallback rules, ledgers, and reporting.
  - `@docs/QMT_documentation.md`: QMT deployment and live-operation workflow.
- When a change materially affects one of these areas, update the relevant document in the same work item. Do not rely on code or logs alone to communicate a changed design, data contract, or operating procedure.
- Keep these documents concise and current. They should describe the latest accepted state, not a chronology of earlier attempts, old names, or superseded workflows.
- Detailed historical traces belong in `@LOGBOOK.md` and `@docs/logbook/DETAILED_LOGBOOK.md`; do not preserve obsolete version labels or migration notes in the living design docs unless they are still operationally relevant.
- Add a new durable document under `docs/` when a new project area becomes important enough that the existing documents would become confusing or overloaded.

## Memory and GPU Monitoring

- Check GPU memory and system memory before and after every script run.
- Stop and adjust the workload if memory usage becomes unsafe.

Recommended checks:

```bash
nvidia-smi
free -h
```

## Execution Workflow

1. Verify free system RAM and GPU memory.
2. Start the job with logging enabled.
3. Recheck memory usage after the job starts and after it finishes.
4. Write the detailed traceability record to `@docs/logbook/DETAILED_LOGBOOK.md`, write the concise result to `@LOGBOOK.md`, and keep `logs/` out of commits.

## Operational Guardrails

- Treat resource checks and logging as mandatory steps, not optional cleanup.
- Keep the repository organized, clean and tidy.
- Prefer fail-fast behavior in core pipelines. Missing data files, cache splits, scaler/meta artifacts, or model weights should raise explicit errors instead of silently falling back.
- When starting a sub-agent, always choose the best performing ones.
- Do not add compatibility or fallback branches unless they are required by a real supported workflow and their trigger conditions are explicit.
- Dare to break thinking inertia and rethink when necessary.
