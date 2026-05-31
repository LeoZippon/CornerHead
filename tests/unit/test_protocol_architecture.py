# Consolidated unit tests: test_protocol_architecture.py


# Source: protocol architecture boundary checks
from __future__ import annotations

import ast
from pathlib import Path
import unittest


class ArchitectureBoundariesTest(unittest.TestCase):
    def test_environment_does_not_import_agent_layer(self):
        environment_root = _repo_root() / "src" / "hl_trader" / "environment"
        violations: list[str] = []
        for path in sorted(environment_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imported = _imported_module(node)
                if imported == "hl_trader.agent" or imported.startswith("hl_trader.agent."):
                    violations.append(f"{path.relative_to(_repo_root())}:{node.lineno}:{imported}")
        self.assertEqual([], violations)

    def test_legacy_top_level_source_packages_have_no_python_modules(self):
        source_root = _repo_root() / "src" / "hl_trader"
        legacy_packages = {
            "agents",
            "backtest",
            "data",
            "evaluation",
            "events",
            "evidence",
            "execution",
            "features",
            "heuristics",
            "leakage",
            "llm",
            "portfolio",
            "protocols",
            "schemas",
            "storage",
            "tracks",
            "wfo",
        }
        leftovers: list[str] = []
        for package in sorted(legacy_packages):
            package_path = source_root / package
            if package_path.exists():
                leftovers.extend(str(path.relative_to(_repo_root())) for path in sorted(package_path.rglob("*.py")))
        self.assertEqual([], leftovers)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _imported_module(node: ast.AST) -> str:
    if isinstance(node, ast.ImportFrom) and node.module:
        return node.module
    if isinstance(node, ast.Import):
        return node.names[0].name if node.names else ""
    return ""


# Source: test_protocol_guards.py
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

from hl_trader.environment.protocols import FreezeSpec, assert_freeze_match, assert_result_available, development_end, development_folds
from hl_trader.environment.schemas import (
    ExperimentConfig,
    HeuristicTemplate,
    HorizonTrack,
    Protocol,
    TradeStrategyPolicy,
)


def config(template_id="template_v1"):
    return ExperimentConfig(
        experiment_id="exp",
        raw_dir=Path("data/raw"),
        feature_dir=Path("data/features"),
        ledger_path=Path(tempfile.gettempdir()) / "ledger.jsonl",
        track=HorizonTrack(
            track_id="horizon_2m",
            target_holding_months=2,
            train_length_months=3,
            test_length_months=2,
            step_months=2,
            template_bank="configs/templates/horizon_2m",
        ),
        protocol=Protocol(
            protocol_id="protocol",
            start_date=date(2020, 1, 1),
            end_date=date(2020, 12, 31),
            heldout_start=date(2020, 8, 1),
        ),
        trade_policy=TradeStrategyPolicy(policy_id="policy"),
        template=HeuristicTemplate(
            template_id=template_id,
            strategy_family="quality_value",
            variable_families=("valuation",),
        ),
    )


class ProtocolGuardsTest(unittest.TestCase):
    def test_development_folds_stop_before_heldout(self):
        cfg = config()
        folds = development_folds(cfg)
        self.assertTrue(folds)
        self.assertEqual(development_end(cfg.protocol), date(2020, 7, 31))
        self.assertTrue(all(fold.test_end < cfg.protocol.heldout_start for fold in folds))

    def test_freeze_hash_detects_template_change(self):
        expected = FreezeSpec.from_config(config("template_v1"))
        observed = FreezeSpec.from_config(config("template_v2"))
        with self.assertRaisesRegex(ValueError, "frozen experiment spec changed"):
            assert_freeze_match(expected, observed)

    def test_freeze_hash_detects_component_content_changes(self):
        cfg = config()
        expected = FreezeSpec.from_config(
            cfg,
            model_id="formulaic_v1",
            prompt_id="prompt_v1",
            data_contract_id="contract_v1",
        )
        changed_specs = {
            "track": FreezeSpec.from_config(
                replace(cfg, track=replace(cfg.track, train_length_months=4)),
                model_id="formulaic_v1",
                prompt_id="prompt_v1",
                data_contract_id="contract_v1",
            ),
            "template": FreezeSpec.from_config(
                replace(cfg, template=replace(cfg.template, parameter_space={"top_n": [10, 20]})),
                model_id="formulaic_v1",
                prompt_id="prompt_v1",
                data_contract_id="contract_v1",
            ),
            "protocol": FreezeSpec.from_config(
                replace(cfg, protocol=replace(cfg.protocol, end_date=date(2021, 1, 31))),
                model_id="formulaic_v1",
                prompt_id="prompt_v1",
                data_contract_id="contract_v1",
            ),
            "policy": FreezeSpec.from_config(
                replace(cfg, trade_policy=replace(cfg.trade_policy, max_daily_turnover_pct=0.4)),
                model_id="formulaic_v1",
                prompt_id="prompt_v1",
                data_contract_id="contract_v1",
            ),
            "model": FreezeSpec.from_config(
                cfg,
                model_id="formulaic_v2",
                prompt_id="prompt_v1",
                data_contract_id="contract_v1",
            ),
            "prompt": FreezeSpec.from_config(
                cfg,
                model_id="formulaic_v1",
                prompt_id="prompt_v2",
                data_contract_id="contract_v1",
            ),
            "data_contract": FreezeSpec.from_config(
                cfg,
                model_id="formulaic_v1",
                prompt_id="prompt_v1",
                data_contract_id="contract_v2",
            ),
        }
        for label, observed in changed_specs.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "frozen experiment spec changed"):
                    assert_freeze_match(expected, observed)

    def test_result_available_time_must_not_pass_train_end(self):
        frame = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ"],
            "result_available_time": ["2020-03-31T18:00:00+08:00", "2020-04-02T18:00:00+08:00"],
        })
        with self.assertRaisesRegex(ValueError, "unavailable results"):
            assert_result_available(frame, train_end=date(2020, 3, 31))

    def test_result_available_time_allows_absent_column_and_yyyymmdd_values(self):
        assert_result_available(pd.DataFrame({"ts_code": ["000001.SZ"]}), train_end=date(2020, 3, 31))
        with self.assertRaisesRegex(ValueError, "missing required result_available_time"):
            assert_result_available(
                pd.DataFrame({"ts_code": ["000001.SZ"]}),
                train_end=date(2020, 3, 31),
                require_column=True,
            )
        frame = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "result_available_time": ["20200331", 20200331, None],
        })
        assert_result_available(frame, train_end=date(2020, 3, 31))
        with self.assertRaisesRegex(ValueError, "missing result_available_time values"):
            assert_result_available(frame, train_end=date(2020, 3, 31), require_column=True)
        late = pd.DataFrame({"result_available_time": [20200401]})
        with self.assertRaisesRegex(ValueError, "unavailable results"):
            assert_result_available(late, train_end=date(2020, 3, 31))
