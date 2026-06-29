"""Structured read-only search tools for Agent exploration.

These tools borrow the useful parts of Claude Code's Grep/Glob design without
opening a general host filesystem search surface. They only read allowlisted
sandbox roots and return paginated, budgeted observations.
"""

from __future__ import annotations

import os
import fnmatch
import selectors
import subprocess
import time
from pathlib import Path, PurePosixPath

from .base import ActionField, ActionSpec, ToolContext, ToolError

DEFAULT_GREP_LIMIT = 250
DEFAULT_GLOB_LIMIT = 100
DEFAULT_READ_LIMIT = 2000
MAX_HEAD_LIMIT = 1000
MAX_READ_LIMIT = 5000
MAX_RESULT_CHARS = 20_000
RG_TIMEOUT_SECONDS = 20.0
VCS_DIRS = (".git", ".hg", ".svn", ".bzr", ".jj", ".sl")
SEARCH_ROOTS = (
    "agent",
    "workspace",
    "output",
    "models",
    "snapshot",
    "train",
    "valid",
    "artifacts",
    "parent_output",
    "parent_models",
    "results",
    "steps",
)
GREP_OUTPUT_MODES = ("content", "files", "count")


class StructuredSearchTool:
    name = "structured_search_tool"
    grep_spec = ActionSpec(
        action="grep",
        tool_name=name,
        description=(
            "Search allowlisted sandbox roots with ripgrep and structured pagination. "
            "Use for targeted text/code/log search; choose files/count modes before content when possible."
        ),
        fields=(
            ActionField("pattern", "string", required=True, description="Ripgrep pattern to search for."),
            ActionField(
                "root",
                "string",
                default="agent",
                choices=SEARCH_ROOTS,
                description="Allowlisted sandbox root to search.",
            ),
            ActionField(
                "path",
                "string",
                default="",
                description="Optional relative subpath under root; leave empty to search the whole root.",
            ),
            ActionField(
                "glob",
                "string",
                default="",
                description="Optional ripgrep glob filter such as '*.py' or '**/*.md'.",
            ),
            ActionField(
                "output_mode",
                "string",
                default="files",
                choices=GREP_OUTPUT_MODES,
                description="Return matching files, counts, or content lines.",
            ),
            ActionField(
                "head_limit",
                "integer",
                default=DEFAULT_GREP_LIMIT,
                min_value=1,
                max_value=MAX_HEAD_LIMIT,
                description="Maximum number of paginated matches to return.",
            ),
            ActionField("offset", "integer", default=0, min_value=0, description="Pagination offset."),
            ActionField(
                "context",
                "integer",
                default=0,
                min_value=0,
                max_value=20,
                description="Context lines around content matches; only useful with output_mode='content'.",
            ),
            ActionField("case_insensitive", "boolean", default=False, description="Enable case-insensitive search."),
            ActionField("multiline", "boolean", default=False, description="Enable ripgrep multiline search."),
        ),
        read_only=True,
        destructive=False,
        concurrency_safe=True,
        max_result_chars=MAX_RESULT_CHARS,
        result_policy="paginated_bounded_inline",
        allowed_modes=("fold", "meta_learning"),
    )
    glob_spec = ActionSpec(
        action="glob",
        tool_name=name,
        description=(
            "List files under an allowlisted sandbox root with structured pagination. "
            "Use to discover files by name/pattern before reading or grepping them."
        ),
        fields=(
            ActionField(
                "pattern",
                "string",
                required=True,
                description="File glob pattern such as '*.py' for one directory or '**/*.py' recursively.",
            ),
            ActionField(
                "root",
                "string",
                default="agent",
                choices=SEARCH_ROOTS,
                description="Allowlisted sandbox root to list.",
            ),
            ActionField(
                "path",
                "string",
                default="",
                description="Optional relative subpath under root; leave empty to list from the root.",
            ),
            ActionField(
                "head_limit",
                "integer",
                default=DEFAULT_GLOB_LIMIT,
                min_value=1,
                max_value=MAX_HEAD_LIMIT,
                description="Maximum number of paginated paths to return.",
            ),
            ActionField("offset", "integer", default=0, min_value=0, description="Pagination offset."),
        ),
        read_only=True,
        destructive=False,
        concurrency_safe=True,
        max_result_chars=MAX_RESULT_CHARS,
        result_policy="paginated_bounded_inline",
        allowed_modes=("fold", "meta_learning"),
    )
    read_spec = ActionSpec(
        action="read",
        tool_name=name,
        description=(
            "Read a file under an allowlisted sandbox root with line numbers and pagination. "
            "Prefer this over `shell cat`/`head` for code you will edit (line-numbered, bounded output); "
            "`cat`/`head` stay available for pipelines."
        ),
        fields=(
            ActionField(
                "root", "string", default="agent", choices=SEARCH_ROOTS,
                description="Allowlisted sandbox root the file lives under.",
            ),
            ActionField("path", "string", required=True, description="Relative file path under root."),
            ActionField("offset", "integer", default=0, min_value=0, description="Starting line offset (0-based)."),
            ActionField(
                "limit", "integer", default=DEFAULT_READ_LIMIT, min_value=1, max_value=MAX_READ_LIMIT,
                description="Maximum number of lines to return from offset.",
            ),
        ),
        read_only=True,
        destructive=False,
        concurrency_safe=True,
        max_result_chars=MAX_RESULT_CHARS,
        result_policy="paginated_bounded_inline",
        allowed_modes=("fold", "meta_learning"),
    )

    def __init__(self, ctx: ToolContext, *, timeout_seconds: float = RG_TIMEOUT_SECONDS) -> None:
        self.ctx = ctx
        self.timeout_seconds = timeout_seconds

    def grep(
        self,
        *,
        pattern: str,
        root: str = "agent",
        path: str = "",
        glob: str = "",
        output_mode: str = "files",
        head_limit: int = DEFAULT_GREP_LIMIT,
        offset: int = 0,
        context: int = 0,
        case_insensitive: bool = False,
        multiline: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        if not pattern:
            raise ToolError("grep pattern must not be empty")
        if output_mode not in GREP_OUTPUT_MODES:
            raise ToolError(f"unsupported grep output_mode: {output_mode}")
        target, display_root = self._resolve_search_path(root, path)
        cwd, target_arg = (target.parent, target.name) if target.is_file() else (target, ".")
        args = [
            "rg",
            "--no-heading",
            "--color",
            "never",
            "--max-columns",
            "500",
        ]
        for vcs_dir in VCS_DIRS:
            args.extend(["--glob", f"!{vcs_dir}/**"])
        if glob:
            _validate_relative_pattern(glob, label="glob")
            args.extend(["--glob", glob])
        if output_mode == "files":
            args.append("--files-with-matches")
        elif output_mode == "count":
            args.append("--count-matches")
        else:
            args.append("--line-number")
            if context:
                args.extend(["-C", str(context)])
        if case_insensitive:
            args.append("-i")
        if multiline:
            args.extend(["-U", "--multiline-dotall"])
        args.extend(["-e", pattern, target_arg])

        completed = self._run_rg(args, cwd, max_lines=offset + head_limit + 1, timeout_seconds=timeout_seconds)
        raw_lines = _clean_rg_lines(completed["stdout_lines"])
        stderr = completed["stderr"]
        if completed["exit_code"] not in (0, 1) and not completed["line_limited"]:
            raise ToolError(stderr.strip() or f"ripgrep failed with exit code {completed['exit_code']}")

        if output_mode == "count":
            visible_matches = _sum_count_lines(raw_lines)
            visible_lines, paging = _apply_paging(
                raw_lines, offset=offset, head_limit=head_limit, source_truncated=completed["line_limited"]
            )
            page_matches = _sum_count_lines(visible_lines)
            content, budget = self._apply_result_budget("\n".join(visible_lines), tool_kind="grep_count")
            record = {
                "tool": self.name,
                "tool_spec": self.grep_spec.to_record(),
                "mode": output_mode,
                "root": root,
                "root_path": display_root,
                "path": path,
                "pattern": pattern,
                "glob": glob,
                "num_lines": len(raw_lines),
                "page_matches": page_matches,
                "num_matches_lower_bound": visible_matches,
                "num_matches_known": not completed["line_limited"],
                "content": content,
                "stderr": stderr,
                "timeout": completed["timeout"],
                **paging,
                **budget,
            }
        elif output_mode == "files":
            filenames = raw_lines
            visible_lines, paging = _apply_paging(
                filenames, offset=offset, head_limit=head_limit, source_truncated=completed["line_limited"]
            )
            content, budget = self._apply_result_budget("\n".join(visible_lines), tool_kind="grep_files")
            record = {
                "tool": self.name,
                "tool_spec": self.grep_spec.to_record(),
                "mode": output_mode,
                "root": root,
                "root_path": display_root,
                "path": path,
                "pattern": pattern,
                "glob": glob,
                "num_files": paging["total"],
                "filenames": visible_lines,
                "content": content,
                "stderr": stderr,
                "timeout": completed["timeout"],
                **paging,
                **budget,
            }
        else:
            visible_lines, paging = _apply_paging(
                raw_lines, offset=offset, head_limit=head_limit, source_truncated=completed["line_limited"]
            )
            content, budget = self._apply_result_budget("\n".join(visible_lines), tool_kind="grep_content")
            record = {
                "tool": self.name,
                "tool_spec": self.grep_spec.to_record(),
                "mode": output_mode,
                "root": root,
                "root_path": display_root,
                "path": path,
                "pattern": pattern,
                "glob": glob,
                "num_lines": len(raw_lines),
                "filenames": sorted(_filenames_from_content(raw_lines)),
                "content": content,
                "stderr": stderr,
                "timeout": completed["timeout"],
                **paging,
                **budget,
            }
        self.ctx.trace.emit("grep", record, step_id=self.ctx.current_step_id)
        return record

    def glob(
        self,
        *,
        pattern: str,
        root: str = "agent",
        path: str = "",
        head_limit: int = DEFAULT_GLOB_LIMIT,
        offset: int = 0,
        deadline_monotonic: float | None = None,
    ) -> dict[str, object]:
        _validate_relative_pattern(pattern, label="pattern")
        target, display_root = self._resolve_search_path(root, path)
        if not target.exists():
            raise ToolError(f"glob path does not exist: {root}:{path}")
        if not target.is_dir():
            raise ToolError(f"glob path must be a directory: {root}:{path}")
        files: list[str] = []
        seen_matches = 0
        source_truncated = False
        for candidate in _iter_glob_matches(target, pattern, deadline_monotonic=deadline_monotonic):
            seen_matches += 1
            if seen_matches <= offset:
                continue
            if len(files) >= head_limit:
                source_truncated = True
                break
            files.append(str(candidate.relative_to(target)))
        visible, paging = _apply_paging(
            files,
            offset=0,
            head_limit=head_limit,
            source_truncated=source_truncated,
            total_prefix=max(seen_matches - len(files), 0),
        )
        paging["offset"] = offset
        content, budget = self._apply_result_budget("\n".join(visible), tool_kind="glob")
        record = {
            "tool": self.name,
            "tool_spec": self.glob_spec.to_record(),
            "root": root,
            "root_path": display_root,
            "path": path,
            "pattern": pattern,
            "num_files": paging["total"],
            "filenames": visible,
            "content": content,
            **paging,
            **budget,
        }
        self.ctx.trace.emit("glob", record, step_id=self.ctx.current_step_id)
        return record

    def read(
        self,
        *,
        root: str = "agent",
        path: str = "",
        offset: int = 0,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> dict[str, object]:
        if not path:
            raise ToolError("read requires a relative file path under the root")
        target, display_root = self._resolve_search_path(root, path)
        if target.is_dir():
            raise ToolError(f"read path is a directory, not a file: {root}:{path}")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"read failed for {root}:{path}: {exc}") from exc
        # cat -n style line numbering, paginated by line so large files stay bounded.
        numbered = [f"{i}\t{line}" for i, line in enumerate(text.splitlines(), start=1)]
        visible, paging = _apply_paging(numbered, offset=offset, head_limit=limit, source_truncated=False)
        content, budget = self._apply_result_budget("\n".join(visible), tool_kind="read")
        record = {
            "tool": self.name,
            "tool_spec": self.read_spec.to_record(),
            "root": root,
            "root_path": display_root,
            "path": path,
            "line_count": len(numbered),
            "content": content,
            **paging,
            **budget,
        }
        self.ctx.trace.emit("read", record, step_id=self.ctx.current_step_id)
        return record

    def _run_rg(
        self,
        args: list[str],
        cwd: Path,
        *,
        max_lines: int,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ToolError("ripgrep executable 'rg' is not available") from exc
        timeout = self.timeout_seconds if timeout_seconds is None else max(0.1, float(timeout_seconds))
        return _collect_rg_lines(proc, timeout_seconds=timeout, max_lines=max_lines)

    def _resolve_search_path(self, root: str, path: str) -> tuple[Path, str]:
        if root not in SEARCH_ROOTS:
            raise ToolError(f"unsupported search root: {root}")
        if root == "models":
            base = self.ctx.paths.model_artifacts
        elif root == "parent_models":
            base = self.ctx.paths.parent_model_artifacts
        else:
            base = getattr(self.ctx.paths, root)
        target = _safe_subpath(base, path)
        if not target.exists():
            raise ToolError(f"search path does not exist: {root}:{path}")
        try:
            display_root = self.ctx.executor.map_path(target) if self.ctx.executor is not None else str(target)
        except Exception:  # noqa: BLE001 - host path is still useful for audit
            display_root = str(target)
        return target, display_root

    def _apply_result_budget(self, content: str, *, tool_kind: str) -> tuple[str, dict[str, object]]:
        if len(content) <= MAX_RESULT_CHARS:
            return content, {"truncated_by_chars": False}
        stored = self.ctx.store_tool_result(tool=self.name, kind=tool_kind, content=content)
        return content[:MAX_RESULT_CHARS], {"truncated_by_chars": True, **stored}


def _safe_subpath(base: Path, path: str) -> Path:
    base_resolved = base.resolve()
    if not path:
        return base_resolved
    candidate = Path(path)
    if candidate.is_absolute():
        raise ToolError("search path must be relative to the selected root")
    parts = PurePosixPath(path).parts
    if ".." in parts:
        raise ToolError("search path must not contain '..'")
    if any(part.startswith(".") for part in parts):
        raise ToolError("search path must not contain hidden path components")
    target = (base_resolved / candidate).resolve()
    _assert_inside(target, base_resolved)
    return target


def _iter_glob_matches(root: Path, pattern: str, *, deadline_monotonic: float | None = None):
    pattern_parts = PurePosixPath(pattern).parts
    stack = [root]
    while stack:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise ToolError("glob timed out before explore deadline")
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ToolError(f"glob failed to list directory: {directory}") from exc
        subdirs: list[Path] = []
        for candidate in entries:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise ToolError("glob timed out before explore deadline")
            if candidate.is_symlink():
                continue
            if _is_vcs_path(candidate, root) or _has_hidden_part(candidate, root):
                continue
            _assert_inside(candidate, root)
            if candidate.is_dir():
                subdirs.append(candidate)
                continue
            if candidate.is_file() and _glob_match(candidate.relative_to(root), pattern_parts):
                yield candidate
        stack.extend(reversed(subdirs))


def _glob_match(relative_path: Path, pattern_parts: tuple[str, ...]) -> bool:
    return _glob_match_parts(tuple(PurePosixPath(relative_path.as_posix()).parts), pattern_parts)


def _glob_match_parts(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts
    first = pattern_parts[0]
    if first == "**":
        return _glob_match_parts(path_parts, pattern_parts[1:]) or (
            bool(path_parts) and _glob_match_parts(path_parts[1:], pattern_parts)
        )
    if not path_parts:
        return False
    return fnmatch.fnmatchcase(path_parts[0], first) and _glob_match_parts(path_parts[1:], pattern_parts[1:])


def _assert_inside(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ToolError(f"path escapes the selected search root: {path}") from exc


def _validate_relative_pattern(pattern: str, *, label: str) -> None:
    if not pattern:
        raise ToolError(f"{label} must not be empty")
    pure = PurePosixPath(pattern)
    if pure.is_absolute() or ".." in pure.parts:
        raise ToolError(f"{label} must be relative and must not contain '..'")
    if any(part.startswith(".") for part in pure.parts):
        raise ToolError(f"{label} must not contain hidden path components")


def _apply_paging(
    lines: list[str],
    *,
    offset: int,
    head_limit: int,
    source_truncated: bool = False,
    total_prefix: int = 0,
) -> tuple[list[str], dict[str, object]]:
    available_len = len(lines)
    total_lower_bound = total_prefix + len(lines)
    if offset > available_len:
        visible: list[str] = []
    else:
        visible = lines[offset : offset + head_limit]
    truncated = source_truncated or offset + len(visible) < available_len
    total = None if source_truncated else total_lower_bound
    return visible, {
        "offset": offset,
        "head_limit": head_limit,
        "returned": len(visible),
        "truncated": truncated,
        "total": total,
        "total_lower_bound": total_lower_bound,
        "total_known": not source_truncated,
    }


def _clean_rg_lines(lines: list[str]) -> list[str]:
    cleaned = []
    for line in lines:
        if line.startswith("./"):
            line = line[2:]
        cleaned.append(line)
    return cleaned


def _sum_count_lines(lines: list[str]) -> int:
    total = 0
    for line in lines:
        _, _, count_text = line.rpartition(":")
        try:
            total += int(count_text)
        except ValueError:
            pass
    return total


def _filenames_from_content(lines: list[str]) -> set[str]:
    filenames: set[str] = set()
    for line in lines:
        if not line or line == "--":
            continue
        separator = line.find(":")
        if separator <= 0:
            continue
        filenames.add(line[:separator])
    return filenames


def _collect_rg_lines(
    proc: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    max_lines: int,
) -> dict[str, object]:
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None
    assert proc.stderr is not None
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    stdout_lines: list[str] = []
    pending = bytearray()
    stderr = bytearray()
    timeout = False
    line_limited = False
    deadline = time.monotonic() + timeout_seconds

    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timeout = True
            proc.kill()
            break
        events = selector.select(timeout=min(0.1, remaining))
        if not events and proc.poll() is not None:
            events = selector.select(timeout=0)
        for key, _ in events:
            chunk = os.read(key.fileobj.fileno(), 8192)
            if not chunk:
                if key.data == "stdout" and pending:
                    line_limited = _append_rg_line(stdout_lines, pending, max_lines=max_lines) or line_limited
                    pending = bytearray()
                selector.unregister(key.fileobj)
                continue
            if key.data == "stderr":
                if len(stderr) < MAX_RESULT_CHARS:
                    stderr.extend(chunk[: MAX_RESULT_CHARS - len(stderr)])
                continue
            pending.extend(chunk)
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending = bytearray(rest)
                if _append_rg_line(stdout_lines, line, max_lines=max_lines):
                    line_limited = True
                    proc.terminate()
                    break
            if line_limited:
                break
        if line_limited:
            break

    for key in list(selector.get_map().values()):
        try:
            selector.unregister(key.fileobj)
        except Exception:
            pass
    selector.close()
    if timeout:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        _close_process_pipes(proc)
        return {
            "exit_code": 124,
            "stdout_lines": stdout_lines,
            "stderr": (stderr.decode("utf-8", errors="replace") or f"timeout after {timeout_seconds}s"),
            "timeout": True,
            "line_limited": line_limited,
        }
    if line_limited:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        _close_process_pipes(proc)
        return {
            "exit_code": 0,
            "stdout_lines": stdout_lines,
            "stderr": stderr.decode("utf-8", errors="replace"),
            "timeout": False,
            "line_limited": True,
        }
    return_code = proc.wait()
    _close_process_pipes(proc)
    return {
        "exit_code": return_code,
        "stdout_lines": stdout_lines,
        "stderr": stderr.decode("utf-8", errors="replace"),
        "timeout": False,
        "line_limited": False,
    }


def _append_rg_line(lines: list[str], line: bytes | bytearray, *, max_lines: int) -> bool:
    if len(lines) >= max_lines:
        return True
    lines.append(bytes(line).decode("utf-8", errors="replace").rstrip("\r"))
    return False


def _close_process_pipes(proc: subprocess.Popen[bytes]) -> None:
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


def _is_vcs_path(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in VCS_DIRS for part in parts)


def _has_hidden_part(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part.startswith(".") for part in parts)
