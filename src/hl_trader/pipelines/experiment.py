from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from hl_trader.agent import FormulaicParameters, parameter_grid
from hl_trader.environment.protocols import FreezeSpec, assert_freeze_match, development_end, development_folds
from hl_trader.environment.schemas import ExperimentConfig, load_experiment_config
from hl_trader.environment.storage import ExperimentLedger, ExperimentLedgerContext
from hl_trader.environment.wfo import Fold
from hl_trader.pipelines.formulaic_wfo import FoldRunResult, FormulaicWfoRunner


@dataclass(frozen=True)
class ExperimentRunResult:
    experiment_id: str
    freeze_hash: str
    ledger_path: Path
    development_end: str
    folds: int
    total_fills: int
    median_test_return: float
    median_long_test_return: float
    median_short_test_return: float
    positive_fold_rate: float
    worst_fold_return: float


@dataclass(frozen=True)
class HeldoutRunResult:
    experiment_id: str
    freeze_hash: str
    ledger_path: Path
    treatment: str
    heldout_start: str
    heldout_end: str
    parameters: FormulaicParameters
    test_return: float
    long_test_return: float
    short_test_return: float
    start_equity: float
    end_equity: float
    fills: int


class DailyFormulaicExperimentRunner:
    def __init__(
        self,
        config: ExperimentConfig,
        *,
        model_id: str = "formulaic",
        prompt_id: str = "",
        data_contract_id: str = "tushare_daily_pit_v1",
    ) -> None:
        self.config = config
        self.freeze_spec = FreezeSpec.from_config(
            config,
            model_id=model_id,
            prompt_id=prompt_id,
            data_contract_id=data_contract_id,
        )
        context = ExperimentLedgerContext.from_freeze_spec(
            self.freeze_spec,
            heldout_start=config.protocol.heldout_start.isoformat() if config.protocol.heldout_start else None,
        )
        self.ledger = ExperimentLedger(config.ledger_path, context)

    @classmethod
    def from_config_file(cls, path: str | Path, **kwargs: Any) -> DailyFormulaicExperimentRunner:
        return cls(load_experiment_config(path), **kwargs)

    def run(
        self,
        features: pd.DataFrame,
        *,
        max_folds: int | None = None,
        initial_cash: float = 1_000_000.0,
    ) -> ExperimentRunResult:
        folds = development_folds(self.config)
        if max_folds is not None:
            if max_folds <= 0:
                raise ValueError("max_folds must be positive")
            folds = folds[:max_folds]
        if not folds:
            raise ValueError("no development folds are available before held-out")
        grid = parameter_grid(self.config.template.parameter_space)
        runner = FormulaicWfoRunner(
            self.config.trade_policy,
            cost_model=self.config.protocol.cost_model,
            ledger=self.ledger,
        )
        self.ledger.append_event(
            "experiment_start",
            payload={
                "freeze_spec": self.freeze_spec.to_record(),
                "development_end": development_end(self.config.protocol).isoformat(),
                "fold_count": len(folds),
                "grid_size": len(grid),
            },
        )
        results: list[FoldRunResult] = []
        for fold in folds:
            assert_freeze_match(self.freeze_spec, FreezeSpec.from_config(
                self.config,
                model_id=self.freeze_spec.model_id,
                prompt_id=self.freeze_spec.prompt_id,
                data_contract_id=self.freeze_spec.data_contract_id,
            ))
            self._append_fold_start(fold)
            params, train_score = runner.fit_parameters(features, fold, grid)
            result = runner.run_fold(features, fold, params, initial_cash=initial_cash)
            result = FoldRunResult(
                fold_id=result.fold_id,
                parameters=result.parameters,
                train_score=train_score,
                test_start=result.test_start,
                test_end=result.test_end,
                start_equity=result.start_equity,
                end_equity=result.end_equity,
                fills=result.fills,
                short_theoretical_return=result.short_theoretical_return,
                short_cash_collateral=result.short_cash_collateral,
                long_return=result.long_return,
            )
            results.append(result)
            self.ledger.append_event(
                "fold_result",
                fold_id=fold.fold_id,
                parameters=asdict(params),
                metrics={
                    "train_score": float(train_score),
                    "test_return": float(result.test_return),
                    "long_test_return": float(result.test_long_return),
                    "short_test_return": float(result.test_short_return),
                    "start_equity": float(result.start_equity),
                    "end_equity": float(result.end_equity),
                    "fills": int(result.fills),
                },
            )
        summary = self._summarize(results)
        self.ledger.append_event(
            "experiment_result",
            metrics={
                "folds": summary.folds,
                "total_fills": summary.total_fills,
                "median_test_return": summary.median_test_return,
                "median_long_test_return": summary.median_long_test_return,
                "median_short_test_return": summary.median_short_test_return,
                "positive_fold_rate": summary.positive_fold_rate,
                "worst_fold_return": summary.worst_fold_return,
            },
        )
        return summary

    def _append_fold_start(self, fold: Fold) -> None:
        self.ledger.append_event(
            "fold_start",
            fold_id=fold.fold_id,
            payload={
                "train_start": fold.train_start.isoformat(),
                "train_end": fold.train_end.isoformat(),
                "test_start": fold.test_start.isoformat(),
                "test_end": fold.test_end.isoformat(),
            },
        )

    def _summarize(self, results: list[FoldRunResult]) -> ExperimentRunResult:
        returns = pd.Series([result.test_return for result in results], dtype="float64")
        long_returns = pd.Series([result.test_long_return for result in results], dtype="float64")
        short_returns = pd.Series([result.test_short_return for result in results], dtype="float64")
        return ExperimentRunResult(
            experiment_id=self.config.experiment_id,
            freeze_hash=self.freeze_spec.freeze_hash,
            ledger_path=self.ledger.path,
            development_end=development_end(self.config.protocol).isoformat(),
            folds=len(results),
            total_fills=sum(result.fills for result in results),
            median_test_return=float(returns.median()) if not returns.empty else 0.0,
            median_long_test_return=float(long_returns.median()) if not long_returns.empty else 0.0,
            median_short_test_return=float(short_returns.median()) if not short_returns.empty else 0.0,
            positive_fold_rate=float((returns > 0).mean()) if not returns.empty else 0.0,
            worst_fold_return=float(returns.min()) if not returns.empty else 0.0,
        )


class DailyFormulaicHeldoutRunner:
    def __init__(
        self,
        config: ExperimentConfig,
        frozen_parameters: FormulaicParameters,
        *,
        treatment: str = "control_formulaic",
        model_id: str = "formulaic",
        prompt_id: str = "",
        data_contract_id: str = "tushare_daily_pit_v1",
    ) -> None:
        if config.protocol.heldout_start is None:
            raise ValueError("held-out runner requires protocol.heldout_start")
        if not treatment:
            raise ValueError("treatment cannot be empty")
        self.config = config
        self.frozen_parameters = frozen_parameters
        self.treatment = treatment
        self.freeze_spec = FreezeSpec.from_config(
            config,
            model_id=model_id,
            prompt_id=prompt_id,
            data_contract_id=data_contract_id,
        )
        context = ExperimentLedgerContext.from_freeze_spec(
            self.freeze_spec,
            heldout_start=config.protocol.heldout_start.isoformat(),
        )
        self.ledger = ExperimentLedger(config.ledger_path, context, default_phase="heldout")

    @classmethod
    def from_config_file(
        cls,
        path: str | Path,
        frozen_parameters: FormulaicParameters,
        **kwargs: Any,
    ) -> DailyFormulaicHeldoutRunner:
        return cls(load_experiment_config(path), frozen_parameters, **kwargs)

    def run(
        self,
        features: pd.DataFrame,
        *,
        initial_cash: float = 1_000_000.0,
    ) -> HeldoutRunResult:
        heldout_start = self.config.protocol.heldout_start
        if heldout_start is None:
            raise ValueError("held-out runner requires protocol.heldout_start")
        if heldout_start > self.config.protocol.end_date:
            raise ValueError("heldout_start cannot be after protocol.end_date")
        fold = Fold(
            "heldout_001",
            self.config.protocol.start_date,
            heldout_start - timedelta(days=1),
            heldout_start,
            self.config.protocol.end_date,
        )
        runner = FormulaicWfoRunner(
            self.config.trade_policy,
            cost_model=self.config.protocol.cost_model,
            ledger=self.ledger,
        )
        self.ledger.append_event(
            "heldout_start",
            phase="heldout",
            parameters=asdict(self.frozen_parameters),
            payload={
                "treatment": self.treatment,
                "freeze_spec": self.freeze_spec.to_record(),
                "heldout_start": heldout_start.isoformat(),
                "heldout_end": self.config.protocol.end_date.isoformat(),
            },
        )
        result = runner.run_fold(features, fold, self.frozen_parameters, initial_cash=initial_cash)
        heldout = HeldoutRunResult(
            experiment_id=self.config.experiment_id,
            freeze_hash=self.freeze_spec.freeze_hash,
            ledger_path=self.ledger.path,
            treatment=self.treatment,
            heldout_start=heldout_start.isoformat(),
            heldout_end=self.config.protocol.end_date.isoformat(),
            parameters=self.frozen_parameters,
            test_return=float(result.test_return),
            long_test_return=float(result.test_long_return),
            short_test_return=float(result.test_short_return),
            start_equity=float(result.start_equity),
            end_equity=float(result.end_equity),
            fills=int(result.fills),
        )
        self.ledger.append_event(
            "heldout_result",
            phase="heldout",
            parameters=asdict(self.frozen_parameters),
            metrics={
                "test_return": heldout.test_return,
                "long_test_return": heldout.long_test_return,
                "short_test_return": heldout.short_test_return,
                "start_equity": heldout.start_equity,
                "end_equity": heldout.end_equity,
                "fills": heldout.fills,
            },
            payload={"treatment": self.treatment},
        )
        return heldout


def read_feature_frame(path: str | Path) -> pd.DataFrame:
    input_path = Path(path)
    if input_path.is_dir():
        files = sorted(input_path.glob("*.parquet"))
        if not files:
            files = sorted(input_path.glob("feature_date=*.parquet"))
        if not files:
            raise FileNotFoundError(f"feature directory contains no parquet files: {input_path}")
        return pd.concat((pd.read_parquet(file) for file in files), ignore_index=True)
    suffix = input_path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(input_path)
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(input_path)
    if suffix in {".json", ".jsonl"}:
        return pd.read_json(input_path, lines=suffix == ".jsonl")
    raise ValueError(f"unsupported feature file type: {input_path.suffix}")
