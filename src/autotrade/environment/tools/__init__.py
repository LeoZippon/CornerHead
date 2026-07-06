"""Public tool exports.

Heavy tool modules are imported lazily so internal helpers can reuse
``tools.base`` without pulling the backtest/NL stack into a circular import.
"""

from __future__ import annotations

from .base import PHASE_FROZEN, PHASE_TRAIN_VALID, ToolContext, ToolError

__all__ = [
    "AgentWebFetchTool",
    "AgentWebSearchTool",
    "BacktestTool",
    "FinishFoldTool",
    "ModificationCheckTool",
    "PHASE_FROZEN",
    "PHASE_TRAIN_VALID",
    "SandboxShellTool",
    "ShellResult",
    "StructuredSearchTool",
    "ToolContext",
    "ToolError",
]


def __getattr__(name: str):
    if name == "AgentWebFetchTool":
        from .web_fetch import AgentWebFetchTool

        return AgentWebFetchTool
    if name == "AgentWebSearchTool":
        from .web_search import AgentWebSearchTool

        return AgentWebSearchTool
    if name == "BacktestTool":
        from .backtest import BacktestTool

        return BacktestTool
    if name == "FinishFoldTool":
        from .finish_fold import FinishFoldTool

        return FinishFoldTool
    if name == "ModificationCheckTool":
        from .modification_check import ModificationCheckTool

        return ModificationCheckTool
    if name == "SandboxShellTool":
        from .shell import SandboxShellTool

        return SandboxShellTool
    if name == "ShellResult":
        from .shell import ShellResult

        return ShellResult
    if name == "StructuredSearchTool":
        from .search import StructuredSearchTool

        return StructuredSearchTool
    raise AttributeError(name)
