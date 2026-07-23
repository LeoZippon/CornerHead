"""Background fold-analysis regeneration for the HITL console.

Task control (pending-set + worker threads) for the LLM strategy analysis —
process/state management like ``ExperimentManager``, kept out of the HTTP
route module. Results land in the analysis sidecar files; failures are
recorded there by ``analyze_fold`` itself.
"""

from __future__ import annotations

import threading
from pathlib import Path

from autotrade.environment.step_tree import StepTree
from autotrade.pipelines.fold_analysis import analyze_fold, analyze_step
from autotrade.pipelines.hitl_state import ANALYSIS_DIR_NAME, HITL_DIR_NAME, PARAMS_NAME, read_json

from . import registry
from .manager import ManagerError


class AnalysisService:
    """Background (re)generation of fold analyses, one at a time per fold."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)
        self._pending: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()

    def pending(self, experiment_id: str, epoch_id: str, fold_id: str) -> bool:
        with self._lock:
            return (experiment_id, epoch_id, fold_id) in self._pending

    def pending_for_experiment(self, experiment_id: str) -> bool:
        """Whether ANY analysis for this experiment is still in flight; its
        worker thread writes under experiments/<id>/hitl/analysis/, so the
        manager refuses to delete the experiment until this drains."""
        with self._lock:
            return any(key[0] == experiment_id for key in self._pending)

    def regenerate(self, experiments_root: Path, experiment_id: str, epoch_id: str, fold_id: str) -> None:
        key = (experiment_id, epoch_id, fold_id)
        with self._lock:
            if key in self._pending:
                raise ManagerError("analysis for this fold is already being generated")
            self._pending.add(key)
        try:
            experiment_dir = registry.resolve_experiment_dir(experiments_root, experiment_id)
            detail = registry.fold_detail(experiments_root, experiment_id, epoch_id, fold_id)
            strategy_dir = detail.get("strategy_dir")
            if not strategy_dir or not Path(str(strategy_dir)).is_dir():
                raise ManagerError("fold has no frozen strategy artifact on disk")
            params = read_json(experiment_dir / HITL_DIR_NAME / PARAMS_NAME)
            model = str(params.get("analysis_model") or "deepseek-v4-pro")
            max_tokens = int(params.get("analysis_max_tokens") or 6000)
            record = dict(detail["record"])
            model_dir = record.get("frozen_model_artifact_path")
        except Exception:
            with self._lock:
                self._pending.discard(key)
            raise

        def _run() -> None:
            try:
                from autotrade.environment.llm import DeepSeekProxy

                proxy = DeepSeekProxy.from_env(
                    model=model,
                    env_file=str(self.repo_root / ".env"),
                    thinking_enabled=True,
                    reasoning_effort="high",
                )
                analyze_fold(
                    proxy,
                    ledger_record=record,
                    strategy_dir=Path(str(strategy_dir)),
                    model_dir=Path(str(model_dir)) if model_dir else None,
                    out_dir=experiment_dir / HITL_DIR_NAME / ANALYSIS_DIR_NAME,
                    max_tokens=max_tokens,
                )
            except Exception:  # noqa: BLE001 - failure lands in the sidecar json
                pass
            finally:
                with self._lock:
                    self._pending.discard(key)

        threading.Thread(target=_run, name=f"analysis-{experiment_id}-{fold_id}", daemon=True).start()

    def regenerate_step(
        self,
        *,
        experiment_dir: Path,
        experiment_id: str,
        node_id: str,
        node_dir: Path,
        status: dict[str, object],
    ) -> None:
        """Generate an optional researcher-only review of the current Step snapshot."""
        key = (experiment_id, "step", node_id)
        with self._lock:
            if key in self._pending:
                raise ManagerError("analysis for this Step is already being generated")
            self._pending.add(key)
        try:
            strategy_dir = Path(node_dir) / "output"
            if not strategy_dir.is_dir():
                raise ManagerError("current Step has no strategy snapshot on disk")
            model_dir = Path(node_dir) / "models"
            params = read_json(Path(experiment_dir) / HITL_DIR_NAME / PARAMS_NAME)
            model = str(params.get("analysis_model") or "deepseek-v4-pro")
            max_tokens = int(params.get("analysis_max_tokens") or 6000)
            node = StepTree(Path(node_dir).parent).get_node(node_id)
            step_index = status.get("awaiting_step")
            step_record: dict[str, object] = {
                "epoch_id": status.get("epoch_id"),
                "fold_id": status.get("fold_id"),
                "step_id": f"step_{int(step_index):03d}" if step_index is not None else None,
                "validation_result": dict(node.get("metrics") or status.get("step_summary") or {}),
                "selected_step_id": node.get("result_name"),
            }
        except Exception:
            with self._lock:
                self._pending.discard(key)
            raise

        def _run() -> None:
            try:
                from autotrade.environment.llm import DeepSeekProxy

                proxy = DeepSeekProxy.from_env(
                    model=model,
                    env_file=str(self.repo_root / ".env"),
                    thinking_enabled=True,
                    reasoning_effort="high",
                )
                analyze_step(
                    proxy,
                    step_record=step_record,
                    strategy_dir=strategy_dir,
                    model_dir=model_dir if model_dir.is_dir() else None,
                    out_dir=Path(experiment_dir) / HITL_DIR_NAME / ANALYSIS_DIR_NAME,
                    node_id=node_id,
                    max_tokens=max_tokens,
                )
            except Exception:  # noqa: BLE001 - failure lands in the sidecar json
                pass
            finally:
                with self._lock:
                    self._pending.discard(key)

        threading.Thread(target=_run, name=f"analysis-{experiment_id}-{node_id}", daemon=True).start()
