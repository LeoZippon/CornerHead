"""Dedicated artifact write/edit tools (Claude-Code edit-tool pattern).

Promoting artifact writes from opaque ``shell`` strings to typed ``write_file`` /
``edit_file`` tools gives the harness a typed, audited record of every formal
change and a deterministic write boundary (``workspace``/``output``/``models``),
plus a staleness check: ``edit_file`` rejects an ``old_string`` that does not
match the current file. Binary model weights are still written with shell/python.
"""

from __future__ import annotations

from pathlib import Path

from autotrade.environment.runtime import AGENT_TOP_LEVEL
from autotrade.environment.artifacts import READONLY_FILES

from .base import ActionField, ActionSpec, ToolContext, ToolError

MAX_WRITE_CHARS = 200_000
# The agent-writable roots; single-sourced with the collection loop and the shell
# write guard (see runtime.AGENT_TOP_LEVEL / SandboxPaths.writable_root_map).
WRITE_ROOT_CHOICES = AGENT_TOP_LEVEL


class ArtifactIOTool:
    name = "artifact_io_tool"
    write_spec = ActionSpec(
        action="write_file",
        tool_name=name,
        description=(
            "在 workspace/output/models 下创建或覆盖一个文本文件（正式代码/配置/轻量元数据）。"
            "二进制模型权重仍用 shell/python 写入 models。"
        ),
        fields=(
            ActionField(
                "root",
                "string",
                required=True,
                choices=WRITE_ROOT_CHOICES,
                description="Writable sandbox root: workspace for drafts, output for formal code, models for text metadata.",
            ),
            ActionField(
                "path",
                "string",
                required=True,
                description="Relative file path under root. Do not use absolute paths or path traversal.",
            ),
            ActionField("content", "string", required=True, description="UTF-8 text content to write."),
        ),
        read_only=False,
        destructive=True,
        concurrency_safe=False,
        allowed_modes=("fold", "meta_learning"),
    )
    edit_spec = ActionSpec(
        action="edit_file",
        tool_name=name,
        description=(
            "对 workspace/output/models 下已存在文本文件做精确字符串替换；"
            "`old_string` 必须与当前文件内容唯一匹配（或设 `replace_all`）。"
        ),
        fields=(
            ActionField(
                "root",
                "string",
                required=True,
                choices=WRITE_ROOT_CHOICES,
                description="Writable sandbox root containing the target text file.",
            ),
            ActionField(
                "path",
                "string",
                required=True,
                description="Relative file path under root. Do not use absolute paths or path traversal.",
            ),
            ActionField(
                "old_string",
                "string",
                required=True,
                description="Exact current text to replace; must match uniquely unless replace_all=true.",
            ),
            ActionField("new_string", "string", required=True, description="Replacement UTF-8 text."),
            ActionField(
                "replace_all",
                "boolean",
                default=False,
                description="Set true only when every occurrence of old_string should be replaced.",
            ),
        ),
        read_only=False,
        destructive=True,
        concurrency_safe=False,
        allowed_modes=("fold", "meta_learning"),
    )

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._roots = ctx.paths.writable_root_map

    def write_file(self, *, root: str, path: str, content: str) -> dict[str, object]:
        self.ctx.require_writable(tool="write_file")
        if len(content) > MAX_WRITE_CHARS:
            raise ToolError(f"content exceeds {MAX_WRITE_CHARS} chars", error_type="too_large")
        target = self._resolve(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
        return self._record(
            "write_file", root, path, target, created=not existed, bytes_written=len(content.encode("utf-8"))
        )

    def edit_file(
        self, *, root: str, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> dict[str, object]:
        self.ctx.require_writable(tool="edit_file")
        target = self._resolve(root, path)
        if not target.exists() or not target.is_file():
            raise ToolError(
                f"file does not exist: {root}/{path}",
                error_type="not_found",
                retry_hint="write_file first, or fix root/path",
            )
        current = target.read_text(encoding="utf-8", errors="replace")
        if old_string == "":
            raise ToolError("old_string cannot be empty", error_type="schema_error")
        count = current.count(old_string)
        if count == 0:
            raise ToolError(
                "old_string not found in file",
                error_type="stale",
                retry_hint="re-read the file; old_string must match the current content exactly",
            )
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_string matched {count} times; pass replace_all=true or make it unique",
                error_type="ambiguous",
            )
        updated = current.replace(old_string, new_string) if replace_all else current.replace(old_string, new_string, 1)
        if len(updated) > MAX_WRITE_CHARS:
            raise ToolError(f"resulting file exceeds {MAX_WRITE_CHARS} chars", error_type="too_large")
        target.write_text(updated, encoding="utf-8")
        return self._record(
            "edit_file",
            root,
            path,
            target,
            replacements=(count if replace_all else 1),
            bytes_written=len(updated.encode("utf-8")),
        )

    def _resolve(self, root: str, path: str) -> Path:
        if root not in self._roots:
            raise ToolError(f"unknown write root: {root!r}", error_type="path_error")
        base = self._roots[root]
        raw_path = str(path).strip()
        if Path(raw_path).is_absolute() or raw_path.startswith("/"):
            raise ToolError(
                "path must be relative to the selected writable root",
                error_type="path_error",
                blocked_target=str(path),
            )
        rel = raw_path
        if not rel:
            raise ToolError("path cannot be empty", error_type="schema_error")
        rel_parts = Path(rel).parts
        if any(part == ".." for part in rel_parts) or any(part.startswith(".") for part in rel_parts):
            raise ToolError(
                "hidden or parent-relative path components are not allowed",
                error_type="path_error",
                blocked_target=str(path),
            )
        resolved = (base / rel).resolve()
        base_resolved = base.resolve()
        if resolved != base_resolved and base_resolved not in resolved.parents:
            raise ToolError("path escapes the writable root", error_type="path_error", blocked_target=str(path))
        if root == "output" and resolved != base_resolved and (
            resolved.relative_to(base_resolved).as_posix() in READONLY_FILES
        ):
            # Compare the RESOLVED relative path: './README.md' and friends must
            # get the documented typed error, not a raw PermissionError later.
            raise ToolError("output/README.md is read-only", error_type="readonly", blocked_target=str(path))
        return resolved

    def _record(self, action: str, root: str, path: str, target: Path, **extra: object) -> dict[str, object]:
        try:
            mapped = self.ctx.executor.map_path(target) if self.ctx.executor is not None else str(target)
        except Exception:  # noqa: BLE001 - host path is still useful for audit
            mapped = str(target)
        return {"tool": self.name, "action": action, "root": root, "path": str(path), "mapped_path": mapped, **extra}
