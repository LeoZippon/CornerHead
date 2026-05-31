# Repository Guidelines

## Environment

- Python Environment is 'stock' installed at `~/miniconda3/envs/stock`.
- Use that installation for environment activation and package management.
- Prefer explicit environment setup over ad hoc local Python changes.
- The real writable repository root on this machine is `/Data/lzp/MacroQuant`.
- Paths under `/home/coder/projects/...` may be wrapper artifacts and should not be trusted for writes.
- Before editing or running commands that write files, confirm the real path with `pwd -P` or `realpath`.

## Git

- The canonical remote repository is already configured.
- Prefer SSH-based Git operations and keep the `origin` remote aligned with the canonical repository.
- Run `git status` before and after changes to confirm the working tree state.
- Pull and rebase or merge carefully before pushing when the remote branch has moved.
- Keep commits focused and descriptive; avoid bundling unrelated changes together.
- Do not rewrite shared history unless the task explicitly requires it and all collaborators are aware.
- Avoid destructive Git commands such as `git reset --hard` and forced pushes unless they are explicitly approved.

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
  - `@docs/data_documentation.md`: data sources, downloads, audits, PIT rules, and unit rules.
  - `@docs/agent_design.md`: evidence packs, LLM shadow, provider adapters, and Agent boundaries.
  - `@docs/environment_design.md`: PIT features, Walk-Forward, replay, execution, and experiment ledgers.
  - `@docs/pipeline_design.md`: feature build, development WFO, held-out, LLM shadow, and cross-layer orchestration.
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
- Keep the repository orgnized, clean and tidy.
- Prefer fail-fast behavior in core pipelines. Missing data files, cache splits, scaler/meta artifacts, or model weights should raise explicit errors instead of silently falling back.
- When starting a sub-agent, always choose the best performing ones.
- Do not add compatibility or fallback branches unless they are required by a real supported workflow and their trigger conditions are explicit.
- Dare to break thinking inertia and rethink when necessary.
