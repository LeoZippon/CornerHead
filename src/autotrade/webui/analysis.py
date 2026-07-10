"""Background fold-analysis regeneration for the HITL console.

Task control (pending-set + worker threads) for the LLM strategy analysis —
process/state management like ``ExperimentManager``, kept out of the HTTP
route module. Results land in the analysis sidecar files; failures are
recorded there by ``analyze_fold`` itself.
"""

from __future__ import annotations

import threading
from pathlib import Path

from autotrade.pipelines.fold_analysis import analyze_fold
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
