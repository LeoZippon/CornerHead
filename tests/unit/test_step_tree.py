import json
import tempfile
import unittest
from pathlib import Path

from autotrade.agent.experiment_facts import build_experiment_facts
from autotrade.agent.prompts import build_meta_learning_prompt, build_system_prompt
from autotrade.environment.artifacts import artifact_hash
from autotrade.environment.runtime import RunManifest
from autotrade.environment.step_tree import StepTree

from .test_artifacts import write_artifact


class StepTreeTest(unittest.TestCase):
    def test_records_nodes_with_parent_lineage_and_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            artifact = write_artifact(tmp / "artifact")
            digest = artifact_hash(artifact)
            tree = StepTree(tmp / "steps")
            node1 = tree.record_step(
                artifact,
                fold_id="fold_2022Q1",
                result_name="valid_000",
                artifact_hash=digest,
                metrics={"total_return": 0.01},
                complete_validation=True,
            )
            node2 = tree.record_step(
                artifact,
                fold_id="fold_2022Q1",
                result_name="valid_001",
                artifact_hash=digest,
                metrics={"total_return": 0.02},
                complete_validation=True,
            )
            reloaded = StepTree(tmp / "steps")
            self.assertEqual(reloaded.current_node_id, node2)
            nodes = {n["node_id"]: n for n in reloaded.nodes()}
            self.assertIsNone(nodes[node1]["parent_node_id"])
            self.assertEqual(nodes[node2]["parent_node_id"], node1)
            self.assertTrue((tmp / "steps" / node1 / "output" / "main.py").exists())
            self.assertEqual(reloaded.position_for_hash(digest), node2)
            rendered = reloaded.render_ascii()
            self.assertIn(node1, rendered)
            self.assertIn("<- current", rendered)
            with self.assertRaisesRegex(ValueError, "already exists"):
                reloaded.record_step(
                    artifact, fold_id="fold_2022Q1", result_name="valid_000",
                    artifact_hash=digest, metrics={}, complete_validation=True,
                )

    def test_run_id_prevents_rerun_node_collisions(self):
        # result_name restarts at valid_000 in every run; a fold re-executed
        # (rerun_fold / post-rollback) must not collide with its earlier run.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            artifact = write_artifact(tmp / "artifact")
            digest = artifact_hash(artifact)
            tree = StepTree(tmp / "steps")
            kwargs = dict(
                epoch_id="epoch_001", fold_id="fold_ref_ab", result_name="valid_000",
                artifact_hash=digest, metrics={}, complete_validation=True,
            )
            node1 = tree.record_step(artifact, run_id="run_x", **kwargs)
            node2 = tree.record_step(artifact, run_id="run_y", **kwargs)
            self.assertNotEqual(node1, node2)
            self.assertEqual(node1, "epoch_001__fold_ref_ab__run_x__valid_000")
            with self.assertRaisesRegex(ValueError, "already exists"):
                tree.record_step(artifact, run_id="run_y", **kwargs)

    def test_epoch_id_prevents_cross_epoch_node_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            artifact = write_artifact(tmp / "artifact")
            digest = artifact_hash(artifact)
            tree = StepTree(tmp / "steps")
            node1 = tree.record_step(
                artifact,
                epoch_id="epoch_001",
                fold_id="fold_2022Q1",
                result_name="valid_000",
                artifact_hash=digest,
                metrics={},
                complete_validation=True,
            )
            node2 = tree.record_step(
                artifact,
                epoch_id="epoch_002",
                fold_id="fold_2022Q1",
                result_name="valid_000",
                artifact_hash=digest,
                metrics={},
                complete_validation=True,
            )
            self.assertNotEqual(node1, node2)
            self.assertIn("epoch_001", node1)
            self.assertIn("epoch_002", node2)

    def test_set_position_validates_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = StepTree(Path(tmp) / "steps")
            with self.assertRaisesRegex(ValueError, "unknown"):
                tree.set_position("nope")
            tree.set_position(None)
            self.assertIsNone(tree.current_node_id)

    def test_failed_attempt_is_dead_end_without_moving_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            artifact = write_artifact(tmp / "artifact")
            digest = artifact_hash(artifact)
            tree = StepTree(tmp / "steps")
            good = tree.record_step(
                artifact, fold_id="fold_2022Q1", result_name="valid_000",
                artifact_hash=digest, metrics={"total_return": 0.01}, complete_validation=True,
            )
            failed = tree.record_failed_attempt(
                fold_id="fold_2022Q1", result_name="failed_abc", error="boom", artifact_hash=digest,
            )
            reloaded = StepTree(tmp / "steps")
            # A failed attempt never becomes the working position or a parent.
            self.assertEqual(reloaded.current_node_id, good)
            nodes = {n["node_id"]: n for n in reloaded.nodes()}
            self.assertEqual(nodes[failed]["parent_node_id"], good)
            self.assertFalse(nodes[failed]["complete_validation"])
            self.assertEqual(nodes[failed]["error"], "boom")
            # No output snapshot is copied for a dead end.
            self.assertFalse((tmp / "steps" / failed).exists())
            # Parent lookup skips failed nodes even when the hash matches.
            self.assertEqual(reloaded.position_for_hash(digest), good)

    def test_save_writes_readable_rendering_with_failed_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            artifact = write_artifact(tmp / "artifact")
            tree = StepTree(tmp / "steps")
            tree.record_step(
                artifact, fold_id="fold_2022Q1", result_name="valid_000",
                artifact_hash=artifact_hash(artifact), metrics={"total_return": 0.01, "sharpe": 1.5},
                complete_validation=True,
            )
            tree.record_failed_attempt(fold_id="fold_2022Q1", result_name="failed_abc", error="boom")
            rendered = (tmp / "steps" / "tree.txt").read_text(encoding="utf-8")
            self.assertIn("valid_000", rendered)
            self.assertIn("ret=0.0100", rendered)
            self.assertIn("sharpe=1.5000", rendered)
            self.assertIn("[failed]", rendered)
            self.assertIn("<- current", rendered)

    def test_attachments_must_not_shadow_snapshot_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            artifact = write_artifact(tmp / "artifact")
            payload = tmp / "detail.json"
            payload.write_text("{}", encoding="utf-8")
            tree = StepTree(tmp / "steps")
            with self.assertRaisesRegex(ValueError, "shadow"):
                tree.record_step(
                    artifact, fold_id="fold_2022Q1", result_name="valid_000",
                    artifact_hash=artifact_hash(artifact), metrics={},
                    complete_validation=True, attachments={"output/x.json": payload},
                )
            node_id = tree.record_step(
                artifact,
                fold_id="fold_2022Q1",
                result_name="valid_001",
                artifact_hash=artifact_hash(artifact),
                metrics={},
                complete_validation=True,
                attachments={"detail.json": payload},
            )
            stored = tree.get_node(node_id)["attachments"]["detail.json"]
            self.assertFalse(Path(stored).is_absolute())
            self.assertEqual(stored, f"{node_id}/detail.json")

    def test_failed_attempt_error_is_redacted_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tree = StepTree(tmp / "steps")
            tree.record_failed_attempt(
                fold_id="fold_2022Q1",
                result_name="failed_secret",
                error="failed Authorization: Bearer secret-token-abc",
            )
            raw = (tmp / "steps" / "tree.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-token-abc", raw)
            payload = json.loads(raw)
            self.assertIn("redacted", payload["nodes"][0]["error"].lower())


class PhasePromptTest(unittest.TestCase):
    def test_phase_and_step_tree_sections(self):
        base = dict(fold_info={"fold_id": "f"}, acceptance_rules={})
        exploration = build_system_prompt(**base)
        self.assertIn("探索期", exploration)
        self.assertNotIn("Step 产物树", exploration)
        convergence = build_system_prompt(**base, phase="convergence", step_tree_enabled=True)
        self.assertIn("收敛期", convergence)
        self.assertIn("不再修改", convergence)
        self.assertIn("/mnt/artifacts/runtime_env.json", convergence)
        self.assertIn("parent_models", convergence)
        self.assertIn("Step 产物树", convergence)
        self.assertIn("tree.txt", convergence)
        self.assertIn("[failed]", convergence)

    def test_fold_directive_section_is_optional_and_framed_as_hypothesis(self):
        base = dict(fold_info={"fold_id": "f"}, acceptance_rules={})
        without = build_system_prompt(**base)
        self.assertNotIn("研究者本 Fold 指令", without)
        with_directive = build_system_prompt(**base, fold_directive="优先检验行业中性化后的动量残差。")
        self.assertIn("研究者本 Fold 指令（用户注入）", with_directive)
        self.assertIn("优先检验行业中性化后的动量残差。", with_directive)
        # Framed as a hypothesis that never relaxes hard constraints.
        self.assertIn("研究假设", with_directive)
        directive_idx = with_directive.index("研究者本 Fold 指令")
        self.assertLess(with_directive.index("# 环境与配置"), directive_idx)
        self.assertLess(directive_idx, with_directive.index("# 动作与流程"))
        # Whitespace-only directives collapse to the no-section prompt.
        self.assertEqual(build_system_prompt(**base, fold_directive="  \n"), without)

    def test_default_fold_exploration_direction_is_additive_and_preserves_autonomy(self):
        prompt = build_system_prompt(
            fold_info={"fold_id": "f"},
            acceptance_rules={},
            fold_exploration_directive="持续检验事件冲击的图传播。",
            fold_directive="本 Fold 先做行业边消融。",
        )
        self.assertIn("实验级默认 Fold 探索方向（用户注入）", prompt)
        self.assertIn("持续检验事件冲击的图传播。", prompt)
        self.assertIn("研究者本 Fold 指令（用户注入）", prompt)
        self.assertIn("本 Fold 先做行业边消融。", prompt)
        self.assertIn("自主提出可证伪假设", prompt)
        self.assertLess(prompt.index("实验级默认 Fold 探索方向"), prompt.index("研究者本 Fold 指令"))

    def test_fold_strategy_interfaces_are_inside_action_section(self):
        prompt = build_system_prompt(fold_info={"fold_id": "f"}, acceptance_rules={})

        environment_idx = prompt.index("# 环境与配置")
        action_idx = prompt.index("# 动作与流程")
        api_idx = prompt.index("## 策略代码接口")
        self.assertGreater(api_idx, action_idx)
        self.assertGreater(action_idx, environment_idx)
        self.assertIn("ctx.broker.cancel", prompt[api_idx:])
        self.assertIn("stale_pending_gt_1m", prompt[api_idx:])

    def test_experiment_facts_replace_raw_fold_schedule(self):
        manifest = {
            "experiment_id": "exp",
            "run_id": "run_x",
            "epoch_id": "epoch_001",
            "fold_id": "fold_2022Q1",
            "kind": "fold",
            "fold": {
                "input_window": "20200101..20210930",
                "validation_period": "20211001..20211231",
                "test_period": "20220101..20220331",
                "test_decision_time": "2022-01-04T09:25:00+08:00",
            },
            "fold_period": "quarter",
            "valid_decision_time": "2021-10-08T09:25:00+08:00",
            "snapshot_config": {
                "decision_windows": {
                    "daily_months": 21,
                    "fundamentals_months": 21,
                    "events_months": 21,
                    "macro_months": 21,
                    "text_months": 21,
                    "intraday_trade_days": 21,
                }
            },
            "acceptance_rules": {"min_return": 0.0},
            "modification_constraints": {"max_changed_lines": 500},
        }
        facts = build_experiment_facts(
            manifest=manifest,
            runtime_env={"python": {"version": "3.11"}, "tools": {"rg": {"available": True}}},
            data_summary={"views": {"snapshot": {"mount_path": "/mnt/snapshot", "files": []}}},
            max_llm_calls=10,
            context_compaction={"enabled": True, "token_threshold": 200000, "max_calls": 8},
            model_artifacts_empty=True,
        )

        prompt = build_system_prompt(
            fold_info=manifest["fold"],
            acceptance_rules={"min_return": 0.0},
            experiment_facts=facts,
        )

        self.assertIn("当前实验事实", prompt)
        self.assertIn("hidden_schedule_redacted", prompt)
        self.assertIn("fold_ref_", prompt)
        self.assertNotIn("fold_2022Q1", prompt)
        self.assertNotIn("test_period", prompt)
        self.assertNotIn("test_decision_time", prompt)
        self.assertNotIn("20220101..20220331", prompt)

    def test_unit_contract_and_test_visibility_are_explicit_by_agent_kind(self):
        unit_contract = {
            "daily.parquet": {"pct_chg_turnover_dv": "decimal; 5%=0.05"},
            "events.parquet": {"moneyflow.*_amount": "10k_CNY; CNY 5m=500"},
        }
        fold_facts = build_experiment_facts(
            manifest={"kind": "fold", "fold_id": "fold_x"},
            data_summary={"unit_contract": unit_contract},
        )
        meta_facts = build_experiment_facts(
            manifest={
                "kind": "meta_learning",
                "epoch_id": "epoch_001",
                "meta_learning_id": "epoch_001_after_fold_003",
                "trigger_after_folds": 3,
            },
            data_summary={"unit_contract": unit_contract},
        )

        self.assertEqual(fold_facts["data_profile"]["unit_contract"], unit_contract)
        self.assertFalse(fold_facts["visibility_policy"]["historical_frozen_test_metrics_visible"])
        self.assertTrue(meta_facts["visibility_policy"]["historical_frozen_test_metrics_visible"])
        self.assertFalse(meta_facts["visibility_policy"]["test_visible"])
        self.assertFalse(meta_facts["visibility_policy"]["heldout_visible"])
        self.assertEqual(meta_facts["identity"]["meta_learning_id"], "epoch_001_after_fold_003")
        self.assertEqual(meta_facts["identity"]["trigger_after_folds"], 3)

        fold_prompt = build_system_prompt(fold_info={"fold_id": "f"}, acceptance_rules={})
        meta_prompt = build_meta_learning_prompt()
        for prompt in (fold_prompt, meta_prompt):
            self.assertIn("5%=0.05", prompt)
            self.assertIn("500", prompt)
            self.assertIn("5%=5.0", prompt)

    def test_fold_facts_opaque_parent_artifact_id(self):
        # Frozen artifact ids embed the raw fold label of the fold that produced
        # them (strategy_<epoch>_fold_<period>), so the facts must project them.
        facts = build_experiment_facts(
            manifest={
                "experiment_id": "exp",
                "run_id": "run_2",
                "epoch_id": "epoch_001",
                "fold_id": "fold_2022Q2",
                "kind": "fold",
                "is_initial_artifact": False,
                "parent_strategy_artifact_id": "strategy_epoch_001_fold_2022Q1",
                "parent_strategy_artifact_hash": "sha256:abc",
            }
        )
        rendered = json.dumps(facts, ensure_ascii=False, sort_keys=True)
        parent = facts["artifact_contract"]["parent"]
        self.assertTrue(str(parent["id"]).startswith("strategy_ref_"))
        self.assertNotIn("fold_2022Q1", rendered)

    def test_proxy_alias_facts_use_active_aliases_only(self):
        requested_only = build_experiment_facts(
            manifest={"kind": "meta_learning"},
            runtime_env={
                "network": "bridge",
                "sandbox_spec": {
                    "env_passthrough": ["GITHUB_TOKEN"],
                    "env_aliases": [
                        {"container_env": "AT_PROXY_HTTP", "host_env": "HTTP_PROXY"},
                    ]
                },
            },
        )
        self.assertNotIn("proxy_alias_names_active", requested_only["runtime_tools"])
        self.assertNotIn("credential_env_names_active", requested_only["runtime_tools"])

        active = build_experiment_facts(
            manifest={
                "kind": "meta_learning",
                "sandbox_runtime": {
                    "active_env_passthrough": ["GITHUB_TOKEN"],
                    "active_env_aliases": [
                        {"container_env": "AT_PROXY_HTTP", "host_env": "HTTP_PROXY"},
                    ]
                },
            },
            runtime_env={
                "network": "bridge",
                "sandbox_spec": {
                    "env_aliases": [
                        {"container_env": "AT_PROXY_HTTP", "host_env": "HTTP_PROXY"},
                    ]
                },
            },
        )
        self.assertEqual(active["runtime_tools"]["credential_env_names_active"], ["GITHUB_TOKEN"])
        self.assertEqual(active["runtime_tools"]["proxy_alias_names_active"], ["AT_PROXY_HTTP"])

    def test_meta_experiment_facts_do_not_inline_sample_dates(self):
        manifest = {
            "experiment_id": "exp",
            "run_id": "run_meta",
            "epoch_id": "epoch_001",
            "fold_id": "epoch_001_meta_learning",
            "kind": "meta_learning",
            "valid_decision_time": "2021-10-08T09:25:00+08:00",
            "experiment_parameters": {
                "fold_period": "quarter",
                "snapshot_config": {"decision_windows": {"daily_months": 21, "intraday_trade_days": 21}},
            },
            "development_inputs": {"development_history": "/mnt/agent/workspace/development_history.json"},
        }
        data_summary = {
            "views": {
                "snapshot": {
                    "mount_path": "/mnt/snapshot",
                    "decision_time": "2021-10-08T09:25:00+08:00",
                    "period_start": "20200101",
                    "period_end": "20210930",
                    "files": [
                        {
                            "path": "daily.parquet",
                            "mount_path": "/mnt/snapshot/daily.parquet",
                            "rows": 10,
                            "date_ranges": {"trade_date": {"min": "20200101", "max": "20210930"}},
                        }
                    ],
                }
            }
        }

        facts = build_experiment_facts(manifest=manifest, data_summary=data_summary)
        rendered = json.dumps(facts, ensure_ascii=False, sort_keys=True)

        self.assertIn("sample_window_only", rendered)
        self.assertNotIn("2021-10-08", rendered)
        self.assertNotIn("20200101", rendered)
        self.assertNotIn("20210930", rendered)

    def test_meta_experiment_facts_are_inside_environment_section(self):
        facts = build_experiment_facts(
            manifest={
                "experiment_id": "exp",
                "run_id": "run_meta",
                "epoch_id": "epoch_001",
                "fold_id": "epoch_001_meta_learning",
                "kind": "meta_learning",
                "development_inputs": {"development_history": "/mnt/agent/workspace/development_history.json"},
            }
        )
        prompt = build_meta_learning_prompt(experiment_facts=facts)

        environment_idx = prompt.index("# 环境与配置")
        facts_idx = prompt.index("## 当前实验事实（可信运行事实，不是交易证据）")
        action_idx = prompt.index("# 动作与流程")
        self.assertGreater(facts_idx, environment_idx)
        self.assertLess(facts_idx, action_idx)

    def test_run_manifest_public_view_redacts_test_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            public_path = Path(tmp) / "artifacts" / "run_manifest.json"
            manifest = RunManifest.create(
                public_path,
                {
                    "kind": "fold",
                    "fold": {
                        "fold_id": "fold_2022Q1",
                        "input_window": "20200101..20210930",
                        "validation_period": "20211001..20211231",
                        "test_period": "20220101..20220331",
                        "test_decision_time": "2022-01-04T09:25:00+08:00",
                    },
                    "test_decision_time": "2022-01-04T09:25:00+08:00",
                    "execution_lag_bars": 2,
                    "decision_max_sim_minutes": 60.0,
                    "backtest_max_seconds_per_decision": 300.0,
                    "backtest_max_seconds_per_trading_day": 900.0,
                    "max_backtests_per_fold": 30,
                    "nl_max_calls_per_decision_day": 10,
                    "snapshots": {
                        "valid_decision_input": {"snapshot_id": "valid"},
                        "valid_replay": {"snapshot_id": "valid_replay"},
                        "test_decision_input": {"snapshot_id": "test"},
                        "test_replay": {"snapshot_id": "test_replay"},
                    },
                    "backtest_summaries": [
                        {"mode": "valid", "total_return": 0.1},
                        {"mode": "frozen_eval", "total_return": 0.2},
                    ],
                },
            )

            public = json.loads(public_path.read_text(encoding="utf-8"))
            host = json.loads(manifest.host_path.read_text(encoding="utf-8"))

            self.assertNotIn("test_decision_time", public)
            self.assertNotIn("test_period", public["fold"])
            self.assertNotIn("test_decision_input", public["snapshots"])
            self.assertNotIn("test_replay", public["snapshots"])
            self.assertEqual([item["mode"] for item in public["backtest_summaries"]], ["valid"])
            self.assertEqual(host["fold"]["test_period"], "20220101..20220331")
            self.assertIn("test_replay", host["snapshots"])
            # Budget/replay config is pure (no test/held-out leak) and is asserted in the
            # prompt facts, so it must survive into the agent-visible manifest too.
            for key in ("execution_lag_bars", "decision_max_sim_minutes", "backtest_max_seconds_per_decision",
                        "backtest_max_seconds_per_trading_day", "max_backtests_per_fold", "nl_max_calls_per_decision_day"):
                self.assertEqual(public[key], manifest.data[key], key)


if __name__ == "__main__":
    unittest.main()
