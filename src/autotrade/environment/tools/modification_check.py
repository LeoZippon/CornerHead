"""modification_check_tool: the deterministic gate before formal backtests.

It checks the ``output/`` strategy artifact directory plus optional ``models/``
parameters, takes no business parameters, verifies parent
copy hashes against the run manifest before diffing, and writes the same result
to agent_trace.jsonl and the run manifest's latest-check summary.
"""

from __future__ import annotations

import ast
from pathlib import Path

from autotrade.environment.artifacts import (
    ArtifactError,
    ModificationConstraints,
    artifact_hash,
    combined_artifact_hash,
    load_model_artifacts,
    load_strategy_artifact,
    modification_delta,
    model_artifact_delta,
    model_artifact_hash,
)
from autotrade.environment.runtime import covering_complete_validation, utc_now_iso

from .base import ActionSpec, ToolContext, ToolError


class ModificationCheckTool:
    name = "modification_check_tool"
    spec = ActionSpec(
        action="modification_check",
        tool_name=name,
        description=(
            "Validate current output/ and models/ artifacts against modification constraints, "
            "parent hashes, size/line limits, and format rules before backtest or finish_fold. "
            "Also returns non-blocking performance/error-handling advisories; advisories never reject code."
        ),
        read_only=False,
        destructive=False,
        concurrency_safe=False,
        allowed_modes=("fold", "meta_learning"),
    )

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def run(self, *, phase: str | None = None) -> dict[str, object]:
        manifest = self.ctx.manifest
        constraints = ModificationConstraints.from_record(dict(manifest.require("modification_constraints")))
        work_root = self.ctx.paths.agent_output
        model_root = self.ctx.paths.model_artifacts
        reasons: list[str] = []

        if constraints.is_initial_artifact:
            base_root = self.ctx.paths.parent_output
            base_model_root = self.ctx.paths.parent_model_artifacts
            # Hard-check the diff base like the parent path: the initial template hash
            # must be present and match, so the modification diff can never be measured
            # against an untrusted base.
            expected = str(manifest.require("initial_template_hash"))
            actual = artifact_hash(base_root)
            if actual != expected:
                raise ToolError(
                    f"initial template hash mismatch: manifest={expected} actual={actual}; diff base is not trusted"
                )
        else:
            base_root = self.ctx.paths.parent_output
            base_model_root = self.ctx.paths.parent_model_artifacts
            expected = str(manifest.require("parent_strategy_artifact_hash"))
            actual = artifact_hash(base_root)
            if actual != expected:
                raise ToolError(
                    f"parent_output hash mismatch: manifest={expected} actual={actual}; diff base is not trusted"
                )
            actual_model = model_artifact_hash(base_model_root)
            # Symmetric fail-fast with the strategy diff base: when a parent model
            # artifact actually exists, its hash must be recorded in the manifest and
            # match. Only an empty/absent parent models root — whose canonical
            # empty-model hash equals ``actual_model`` — may be trusted without the
            # manifest field.
            if load_model_artifacts(base_model_root).files:
                expected_model = str(manifest.require("parent_model_artifact_hash"))
            else:
                expected_model = model_artifact_hash(base_model_root)
            if actual_model != expected_model:
                raise ToolError(
                    "parent_models hash mismatch: "
                    f"manifest={expected_model} actual={actual_model}; model base is not trusted"
                )

        delta = None
        model_delta = None
        current_hash: str | None = None
        current_model_hash: str | None = None
        try:
            load_strategy_artifact(work_root)
            current_hash = artifact_hash(work_root)
            current_model_hash = model_artifact_hash(model_root)
            delta = modification_delta(base_root, work_root)
            model_delta = model_artifact_delta(base_model_root, model_root)
            allowed, reasons = constraints.evaluate(delta, model_delta)
        except ArtifactError as exc:
            allowed = False
            reasons = [f"artifact format invalid: {exc}"]

        summary: dict[str, object] = {
            "tool": self.name,
            "tool_spec": self.spec.to_record(),
            "checked_at": utc_now_iso(),
            "allowed_to_backtest": allowed,
            "artifact_hash": current_hash,
            "model_artifact_hash": current_model_hash,
            "combined_artifact_hash": (
                combined_artifact_hash(current_hash, current_model_hash)
                if current_hash is not None and current_model_hash is not None
                else None
            ),
            "constraints": constraints.to_record(),
            "delta": delta.to_record() if delta is not None else None,
            "model_delta": model_delta.to_record() if model_delta is not None else None,
            "reasons": reasons,
            # Static hints only. They neither change ``allowed`` nor become an
            # acceptance input; the Agent remains free to keep the code.
            "advisories": _strategy_advisories(work_root),
        }
        # After edit-then-revert (or a step_rollback) the Agent cannot compare
        # hashes across a compacted session; say outright whether these exact
        # artifacts already carry a freezable complete validation.
        covered_by = covering_complete_validation(manifest, current_hash, current_model_hash)
        summary["complete_validation_covered"] = covered_by is not None
        summary["covered_by"] = covered_by
        manifest.record_modification_check(summary)
        self.ctx.trace.emit("tool", {**summary}, step_id=self.ctx.current_step_id, phase=phase)
        return summary


_POSITION_ROW_KEYS = frozenset(
    {
        "account",
        "ts_code",
        "side",
        "quantity",
        "sellable_quantity",
        "entry_price",
        "entry_date",
        "entry_cost",
        "last_price",
        "market_value",
    }
)


def _positions_rooted(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Attribute) and child.attr == "positions" for child in ast.walk(node))


def _position_row_names(tree: ast.AST) -> set[str]:
    """Loop/comprehension targets iterating position rows: ``for p in ctx.positions``
    directly, or via a simple alias assigned from a ``.positions``-rooted expression."""
    aliases = {
        target.id
        for stmt in ast.walk(tree)
        if isinstance(stmt, ast.Assign) and _positions_rooted(stmt.value)
        for target in stmt.targets
        if isinstance(target, ast.Name)
    }

    def _iterates_positions(iterable: ast.AST) -> bool:
        return _positions_rooted(iterable) or (isinstance(iterable, ast.Name) and iterable.id in aliases)

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and _iterates_positions(node.iter):
            names.add(node.target.id)
        elif isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name) and _iterates_positions(node.iter):
            names.add(node.target.id)
    return names


def _strategy_advisories(root: Path) -> list[dict[str, object]]:
    advisories: list[dict[str, object]] = []
    for path in sorted(Path(root).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        relative = path.relative_to(root).as_posix()
        position_rows = _position_row_names(tree)
        price_functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _contains_ctx_price(node)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read_parquet":
                columns = next((keyword.value for keyword in node.keywords if keyword.arg == "columns"), None)
                projected = columns is not None and not (
                    isinstance(columns, ast.Constant) and columns.value is None
                )
                if not projected:
                    advisories.append(
                        {
                            "kind": "unprojected_parquet_read",
                            "path": relative,
                            "line": node.lineno,
                            "message": "read_parquet has no columns projection; this may load an entire wide domain",
                        }
                    )
            elif isinstance(node, ast.ExceptHandler) and _is_broad_exception(node.type):
                if not any(isinstance(child, ast.Raise) for statement in node.body for child in ast.walk(statement)):
                    advisories.append(
                        {
                            "kind": "suppressed_broad_exception",
                            "path": relative,
                            "line": node.lineno,
                            "message": "broad exception is not re-raised; preserve enough signal to diagnose empty fallbacks",
                        }
                    )
            elif isinstance(node, ast.If) and _is_blind_auction_time_test(node.test):
                body = ast.Module(body=node.body, type_ignores=[])
                direct_price = _contains_ctx_price(body)
                calls_price_helper = any(
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id in price_functions
                    for child in ast.walk(body)
                )
                if direct_price or calls_price_helper:
                    advisories.append(
                        {
                            "kind": "blind_auction_price_lookup",
                            "path": relative,
                            "line": node.lineno,
                            "message": (
                                "09:15/09:25 are blind auction ticks: ctx.price() returns None; "
                                "size from an earlier reference price or wait for a real 09:30 bar"
                            ),
                        }
                    )
            # Every position row exit-path bug audited so far was a hallucinated
            # key (qty/volume/cost_basis) silently defaulted by dict.get; flag
            # unknown keys on rows that iterate a `.positions` surface.
            key_node = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in position_rows
                and node.args
            ):
                key_node = node.args[0]
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id in position_rows
            ):
                key_node = node.slice
            if (
                key_node is not None
                and isinstance(key_node, ast.Constant)
                and isinstance(key_node.value, str)
                and key_node.value not in _POSITION_ROW_KEYS
            ):
                advisories.append(
                    {
                        "kind": "unknown_position_row_key",
                        "path": relative,
                        "line": node.lineno,
                        "message": (
                            f"position rows have no '{key_node.value}' key (exact keys: "
                            "account, ts_code, side, quantity, sellable_quantity, entry_price, "
                            "entry_date, entry_cost, last_price, market_value); .get() with a "
                            "default silently returns the default and turns exit logic into dead "
                            "code. Ignore if this variable holds your own derived/enriched rows "
                            "rather than raw ctx.positions rows"
                        ),
                    }
                )
    return advisories


def _contains_ctx_price(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "price"
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "ctx"
        for child in ast.walk(node)
    )


def _is_blind_auction_time_test(node: ast.AST) -> bool:
    has_cur_time = any(
        isinstance(child, ast.Attribute)
        and child.attr == "cur_time"
        and isinstance(child.value, ast.Name)
        and child.value.id == "ctx"
        for child in ast.walk(node)
    )
    has_blind_time = any(
        isinstance(child, ast.Constant) and child.value in {"09:15", "09:25"}
        for child in ast.walk(node)
    )
    return has_cur_time and has_blind_time


def _is_broad_exception(node: ast.expr | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in {"Exception", "BaseException"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"Exception", "BaseException"}
    return False
