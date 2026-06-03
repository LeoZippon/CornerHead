# Consolidated unit tests: test_pipeline.py


# Source: test_experiment_runner.py
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from hl_trader.pipelines import DailyFormulaicExperimentRunner, DailyFormulaicHeldoutRunner, read_feature_frame
from hl_trader.agent import FormulaicParameters


def feature_frame(*, include_heldout: bool = False):
    dates = ["20200131", "20200228", "20200331", "20200430", "20200529", "20200630", "20200731"]
    if include_heldout:
        dates.extend(["20200831", "20200930", "20201030"])
    fallback_tradable = "20201130" if include_heldout else "20200831"
    rows = []
    for index, feature_date in enumerate(dates):
        tradable_date = dates[index + 1] if index + 1 < len(dates) else fallback_tradable
        for code_index, code in enumerate(["000001.SZ", "000002.SZ", "000003.SZ"]):
            rows.append({
                "feature_date": feature_date,
                "source_trade_date": feature_date,
                "tradable_date": tradable_date,
                "available_at": f"{feature_date[:4]}-{feature_date[4:6]}-{feature_date[6:8]}T18:00:00+08:00",
                "result_available_time": f"{feature_date[:4]}-{feature_date[4:6]}-{feature_date[6:8]}T18:00:00+08:00",
                "ts_code": code,
                "close": 10.0 + index + code_index,
                "pct_chg": 10.0 if code_index == 0 else 0.5,
                "pe_ttm": 8.0 + code_index * 5,
                "pb": 0.8 + code_index,
                "amount": 100000.0 + code_index * 1000,
                "amount_ma20": 90000.0 + code_index * 1000,
                "ret_20d": 0.02 - code_index * 0.01,
                "up_limit": 999.0,
                "down_limit": 0.01,
                "is_suspended": False,
            })
    return pd.DataFrame(rows)


def config_yaml(path: Path, ledger_path: Path) -> None:
    path.write_text(
        f"""
experiment_id: unit_daily_formulaic
raw_dir: ../../data/raw
feature_dir: ../../data/features
ledger_path: {ledger_path}
track:
  track_id: horizon_2m_quality
  target_holding_months: 2
  train_length_months: 3
  test_length_months: 2
  step_months: 2
  template_bank: configs/templates/horizon_2m
protocol:
  protocol_id: protocol_daily_unit
  start_date: 2020-01-01
  end_date: 2020-12-31
  heldout_start: 2020-08-01
  decision_anchor: month_end
  rebalance_frequency: monthly
  nl_weight: 0.0
trade_policy:
  policy_id: policy_daily_unit
  data_granularity: daily
  settlement_mode: t_plus_1
  max_daily_turnover_pct: 0.5
  max_position_deviation_pct: 0.05
  min_expected_edge_after_cost: 0.0
  allowed_actions: [hold, enter, exit, trim, add, rebalance]
template:
  template_id: template_quality_unit
  strategy_family: quality_value
  variable_families: [valuation, liquidity, momentum]
  parameter_space:
    max_pe_ttm_quantile: [0.7]
    max_pb_quantile: [0.7]
    min_turnover_quantile: [0.0]
    top_n: [2]
  objective: excess_return_after_cost
""",
        encoding="utf-8",
    )


class ExperimentRunnerTest(unittest.TestCase):
    def test_daily_formulaic_runner_writes_contextual_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "experiment.yaml"
            ledger_path = root / "ledger.jsonl"
            config_yaml(config_path, ledger_path)

            runner = DailyFormulaicExperimentRunner.from_config_file(
                config_path,
                model_id="formulaic_unit",
                prompt_id="prompt_unit",
                data_contract_id="contract_unit",
            )
            result = runner.run(feature_frame(), max_folds=1, initial_cash=100000.0)
            records = runner.ledger.read_all()

        self.assertEqual(result.folds, 1)
        self.assertTrue(result.freeze_hash)
        self.assertTrue(any(record["event_type"] == "experiment_start" for record in records))
        self.assertTrue(any(record["event_type"] == "fold_result" for record in records))
        for record in records:
            self.assertEqual(record["experiment_id"], "unit_daily_formulaic")
            self.assertEqual(record["freeze_hash"], result.freeze_hash)
            self.assertEqual(record["phase"], "development")
            self.assertEqual(record["model_id"], "formulaic_unit")
            self.assertEqual(record["prompt_id"], "prompt_unit")
            self.assertEqual(record["data_contract_id"], "contract_unit")
            self.assertTrue(record["track_hash"])
            self.assertTrue(record["template_hash"])
            self.assertTrue(record["protocol_hash"])
            self.assertTrue(record["trade_policy_hash"])

    def test_daily_formulaic_heldout_runner_uses_frozen_params_and_heldout_phase(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "experiment.yaml"
            ledger_path = root / "ledger.jsonl"
            config_yaml(config_path, ledger_path)

            runner = DailyFormulaicHeldoutRunner.from_config_file(
                config_path,
                frozen_parameters=FormulaicParameters(
                    top_n=2,
                    max_pe_ttm_quantile=0.7,
                    max_pb_quantile=0.7,
                    min_amount_quantile=0.0,
                ),
                treatment="control_formulaic",
                model_id="deepseek-v4-pro",
            )
            result = runner.run(feature_frame(include_heldout=True), initial_cash=100000.0)
            records = runner.ledger.read_all()

        self.assertEqual(result.treatment, "control_formulaic")
        self.assertEqual(result.heldout_start, "2020-08-01")
        self.assertEqual(result.parameters.top_n, 2)
        self.assertTrue(any(record["event_type"] == "heldout_start" for record in records))
        self.assertTrue(any(record["event_type"] == "heldout_result" for record in records))
        self.assertFalse(any(record["event_type"] == "experiment_start" for record in records))
        for record in records:
            self.assertEqual(record["phase"], "heldout")
            self.assertEqual(record["model_id"], "deepseek-v4-pro")
            self.assertEqual(record["freeze_hash"], result.freeze_hash)

    def test_read_feature_frame_accepts_partition_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            frame = feature_frame()
            for feature_date, group in frame.groupby("feature_date"):
                group.to_parquet(path / f"feature_date={feature_date}.parquet", index=False)
            loaded = read_feature_frame(path)
        self.assertEqual(len(loaded), len(frame))

    def test_ledger_tamper_is_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "experiment.yaml"
            ledger_path = root / "ledger.jsonl"
            config_yaml(config_path, ledger_path)
            runner = DailyFormulaicExperimentRunner.from_config_file(config_path)
            runner.run(feature_frame(), max_folds=1, initial_cash=100000.0)
            lines = ledger_path.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["event_type"] = "tampered"
            lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True)
            ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "record_hash verification failed"):
                runner.ledger.read_all()

    def test_hl_cli_import_has_no_side_effects(self):
        script_path = Path("scripts/hl.py").resolve()
        spec = importlib.util.spec_from_file_location("hl_cli_test", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))

    def test_fundamental_event_audit_raises_on_error_status(self):
        script_path = Path("scripts/hl.py").resolve()
        spec = importlib.util.spec_from_file_location("hl_cli_test_audit", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_dir = root / "events" / "dividend"
            events_dir.mkdir(parents=True)
            pd.DataFrame([{"dataset": "wrong"}]).to_parquet(events_dir / "available_month=202001.parquet", index=False)
            args = type("Args", (), {
                "events_root": root / "events",
                "start_date": "20200101",
                "end_date": "20200131",
                "dataset": ["dividend"],
                "output": root / "status.json",
            })()
            with self.assertRaisesRegex(ValueError, "fundamental event audit failed"):
                module.run_audit_fundamental_events(args)


# Source: test_formulaic_wfo_runner.py
import unittest
from datetime import date

import pandas as pd

from hl_trader.environment.schemas import TradeStrategyPolicy
from hl_trader.agent import FormulaicParameters, parameter_grid, select_formulaic_candidates
from hl_trader.environment.wfo import Fold, generate_rolling_folds
from hl_trader.pipelines.formulaic_wfo import FormulaicWfoRunner


class ListLedger:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


def synthetic_features():
    rows = []
    dates = pd.date_range("2020-01-31", periods=18, freq="M").strftime("%Y%m%d").tolist()
    next_dates = pd.date_range("2020-02-03", periods=18, freq="M").strftime("%Y%m%d").tolist()
    for i, feature_date in enumerate(dates):
        tradable = next_dates[i]
        for j, code in enumerate(["A", "B", "C", "D"]):
            cheap = j == 0
            price = 10 + i * (1.0 if cheap else 0.1) + j
            rows.append({
                "feature_date": feature_date,
                "source_trade_date": feature_date,
                "tradable_date": tradable,
                "available_at": f"{feature_date[:4]}-{feature_date[4:6]}-{feature_date[6:8]}T18:00:00+08:00",
                "result_available_time": f"{feature_date[:4]}-{feature_date[4:6]}-{feature_date[6:8]}T18:00:00+08:00",
                "ts_code": code,
                "close": price,
                "pct_chg": 10.0 if cheap else 0.5,
                "pe_ttm": 8.0 if cheap else 20.0 + j,
                "pb": 0.8 if cheap else 2.0 + j,
                "ret_20d": 0.05 if cheap else -0.01,
                "amount": 400000.0 if cheap else 100000.0 + j,
                "amount_ma20": 100000.0 + j,
                "up_limit": price * 1.1,
                "down_limit": price * 0.9,
                "is_suspended": False,
            })
    return pd.DataFrame(rows)


class FormulaicWfoRunnerTest(unittest.TestCase):
    def test_formulaic_runner_requires_rebalance_policy_action(self):
        with self.assertRaisesRegex(ValueError, "requires TradeStrategyPolicy.allowed_actions to include rebalance"):
            FormulaicWfoRunner(TradeStrategyPolicy(policy_id="p", allowed_actions=("hold", "enter", "exit")))

    def test_parameter_grid_from_template_space(self):
        grid = parameter_grid({"top_n": [10, 20], "max_pe_ttm_quantile": [0.4], "max_pb_quantile": [0.5]})
        self.assertEqual(len(grid), 2)
        self.assertEqual(grid[0].top_n, 10)

    def test_select_candidates_prefers_cheap_liquid_names(self):
        frame = synthetic_features()
        cross = frame[frame["feature_date"] == frame["feature_date"].min()]
        selected = select_formulaic_candidates(cross, FormulaicParameters(top_n=1))
        self.assertEqual(selected, ["A"])

    def test_run_wfo_returns_fold_results(self):
        features = synthetic_features()
        folds = generate_rolling_folds(
            start_date=date(2020, 1, 31),
            end_date=date(2021, 6, 30),
            train_length_months=9,
            test_length_months=3,
            step_months=3,
        )
        runner = FormulaicWfoRunner(TradeStrategyPolicy(policy_id="p"))
        results = runner.run_wfo(features, folds, [FormulaicParameters(top_n=1)], initial_cash=100_000.0)
        self.assertTrue(results)
        self.assertTrue(all(result.parameters.top_n == 1 for result in results))
        self.assertTrue(all(result.end_equity >= 0 for result in results))

    def test_training_features_must_include_result_available_time(self):
        features = synthetic_features().drop(columns=["result_available_time"])
        fold = Fold(
            fold_id="fold_availability",
            train_start=date(2020, 1, 31),
            train_end=date(2020, 9, 30),
            test_start=date(2020, 10, 1),
            test_end=date(2020, 12, 31),
        )
        runner = FormulaicWfoRunner(TradeStrategyPolicy(policy_id="p"))
        with self.assertRaisesRegex(ValueError, "missing required result_available_time"):
            runner.fit_parameters(features, fold, [FormulaicParameters(top_n=1)])

    def test_execution_constraint_columns_are_required(self):
        features = synthetic_features().drop(columns=["up_limit"])
        fold = Fold(
            fold_id="fold_constraints",
            train_start=date(2020, 1, 31),
            train_end=date(2020, 9, 30),
            test_start=date(2020, 10, 1),
            test_end=date(2020, 12, 31),
        )
        runner = FormulaicWfoRunner(TradeStrategyPolicy(policy_id="p"))
        with self.assertRaisesRegex(ValueError, "missing required feature columns"):
            runner.run_fold(features, fold, FormulaicParameters(top_n=1), initial_cash=100_000.0)

    def test_allowed_actions_can_disable_enter_orders(self):
        features = synthetic_features()
        fold = Fold(
            fold_id="fold_no_enter",
            train_start=date(2020, 1, 31),
            train_end=date(2020, 9, 30),
            test_start=date(2020, 10, 1),
            test_end=date(2020, 12, 31),
        )
        policy = TradeStrategyPolicy(policy_id="p", allowed_actions=("hold", "exit", "trim", "rebalance", "event_de_risk"))
        runner = FormulaicWfoRunner(policy)
        result = runner.run_fold(features, fold, FormulaicParameters(top_n=1), initial_cash=100_000.0)
        self.assertEqual(result.fills, 0)

    def test_event_checkpoints_are_logged_with_action_eligibility(self):
        features = synthetic_features()
        features.loc[(features["feature_date"] == "20201031") & (features["ts_code"] == "A"), "pct_chg"] = -10.0
        fold = Fold(
            fold_id="fold_events",
            train_start=date(2020, 1, 31),
            train_end=date(2020, 9, 30),
            test_start=date(2020, 10, 1),
            test_end=date(2020, 12, 31),
        )
        ledger = ListLedger()
        runner = FormulaicWfoRunner(TradeStrategyPolicy(policy_id="p"), ledger=ledger)
        runner.run_fold(features, fold, FormulaicParameters(top_n=1), initial_cash=100_000.0)
        events = [event for event in ledger.events if event["event_type"] == "event_checkpoint"]
        self.assertTrue(events)
        self.assertTrue(all(event["action"] == "eligible" for event in events))
        self.assertTrue(all(event["action_impact"] == "execution_policy" for event in events))
        self.assertTrue(any(event["can_affect_trading"] is True for event in events))
        self.assertTrue(any(event["can_affect_trading"] is False for event in events))

    def test_negative_event_can_de_risk_existing_position(self):
        features = synthetic_features()
        features.loc[(features["feature_date"] == "20201130") & (features["ts_code"] == "A"), "pct_chg"] = -10.0
        fold = Fold(
            fold_id="fold_event_trade",
            train_start=date(2020, 1, 31),
            train_end=date(2020, 9, 30),
            test_start=date(2020, 10, 1),
            test_end=date(2021, 1, 31),
        )
        ledger = ListLedger()
        policy = TradeStrategyPolicy(policy_id="p", max_daily_turnover_pct=0.8, event_de_risk_pct=0.5)
        runner = FormulaicWfoRunner(policy, ledger=ledger)
        result = runner.run_fold(features, fold, FormulaicParameters(top_n=1), initial_cash=100_000.0)
        event_actions = [event for event in ledger.events if event["event_type"] == "event_action"]
        event_fills = [
            event for event in event_actions
            if event["action"] == "event_de_risk" and event["filled"] is True
        ]
        self.assertGreater(result.fills, 0)
        self.assertTrue(event_fills)
        self.assertTrue(any(event["fill"].side == "sell" for event in event_fills))

    def test_run_fold_skips_rebalance_outside_test_window(self):
        features = synthetic_features()
        final_feature = features["feature_date"] == "20201231"
        features.loc[final_feature & (features["ts_code"] == "A"), ["pe_ttm", "pb", "ret_20d"]] = [40.0, 5.0, -0.2]
        features.loc[final_feature & (features["ts_code"] == "B"), ["pe_ttm", "pb", "ret_20d"]] = [6.0, 0.6, 0.2]
        fold = Fold(
            fold_id="fold_check",
            train_start=date(2020, 1, 31),
            train_end=date(2020, 9, 30),
            test_start=date(2020, 10, 1),
            test_end=date(2020, 12, 31),
        )
        ledger = ListLedger()
        runner = FormulaicWfoRunner(TradeStrategyPolicy(policy_id="p"), ledger=ledger)
        result = runner.run_fold(features, fold, FormulaicParameters(top_n=1), initial_cash=100_000.0)
        fill_events = [event for event in ledger.events if event["event_type"] == "fill"]
        self.assertGreater(result.fills, 0)
        self.assertTrue(all(event["fill"].trade_date <= fold.test_end for event in fill_events))
        self.assertTrue(any(
            event["event_type"] == "rebalance_skipped" and event["reason"] == "tradable_date_outside_test_window"
            for event in ledger.events
        ))

    def test_duplicate_prices_fail_fast(self):
        features = synthetic_features()
        duplicate_row = features[features["feature_date"] == "20201031"].iloc[[0]]
        duplicated = pd.concat([features, duplicate_row], ignore_index=True)
        fold = Fold(
            fold_id="fold_dup",
            train_start=date(2020, 1, 31),
            train_end=date(2020, 9, 30),
            test_start=date(2020, 10, 1),
            test_end=date(2020, 12, 31),
        )
        runner = FormulaicWfoRunner(TradeStrategyPolicy(policy_id="p"))
        with self.assertRaisesRegex(ValueError, "duplicate price rows"):
            runner.run_fold(duplicated, fold, FormulaicParameters(top_n=1), initial_cash=100_000.0)


# Source: test_wfo_splitter.py
import unittest
from datetime import date

from hl_trader.environment.wfo import generate_rolling_folds, month_add


class WfoSplitterTest(unittest.TestCase):
    def test_month_add_handles_month_end(self):
        self.assertEqual(month_add(date(2020, 1, 31), 1), date(2020, 2, 29))
        self.assertEqual(month_add(date(2021, 1, 31), 1), date(2021, 2, 28))

    def test_generate_rolling_folds(self):
        folds = generate_rolling_folds(
            start_date=date(2020, 1, 1),
            end_date=date(2021, 12, 31),
            train_length_months=12,
            test_length_months=3,
            step_months=3,
        )
        self.assertEqual(folds[0].train_start, date(2020, 1, 1))
        self.assertEqual(folds[0].train_end, date(2020, 12, 31))
        self.assertEqual(folds[0].test_start, date(2021, 1, 1))
        self.assertEqual(folds[0].test_end, date(2021, 3, 31))
        self.assertTrue(all(f.train_end < f.test_start for f in folds))
