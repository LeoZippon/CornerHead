# Repository Guidelines

## Environment

- Miniconda is 'stock' installed at `~/miniconda3`.
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
- Use `@SUMMARY.md` as the concise current logbook: keep entries short and focused on what was tried, the key result, and the current conclusion.
- Use `@docs/summaries/SUMMARY.original.md` as the detailed durable logbook: include the date, task, key command or config, resource checks, important artifact/log paths, and final result or conclusion.
- For routine context gathering, read `@SUMMARY.md` first. Read `@docs/summaries/SUMMARY.original.md` only when detailed historical commands, paths, or experiment context are needed.
- Runtime log files under `logs/` may be kept locally for debugging, but they must not be committed to Git.
- If a script supports file logging, you may save logs to disk locally, but durable summaries in Git must still be written back to both the concise and detailed logbooks at the appropriate granularity.

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
4. Write the detailed traceability record to `@docs/summaries/SUMMARY.original.md`, write the concise result to `@SUMMARY.md`, and keep `logs/` out of commits.

## Operational Guardrails

- Treat resource checks and logging as mandatory steps, not optional cleanup.
- Prefer fail-fast behavior in core pipelines. Missing data files, cache splits, scaler/meta artifacts, or model weights should raise explicit errors instead of silently falling back.
- Do not add compatibility or fallback branches unless they are required by a real supported workflow and their trigger conditions are explicit.
- Dare to break thinking inertia and rethink when necessary.
