"""Session-runner regressions: step-gate correctness, the deterministic-trim
pressure valve when semantic compaction is disabled, and prompt-export
reproducibility (configs/prompts/PROMPTS.md must equal the generator output).
"""
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrade.agent import AgentSessionConfig, AgentSessionRunner
from autotrade.environment.llm.proxy import ScriptedLLM

from .fixtures_sandbox import REPO_ROOT
from .test_tools_flow import build_sandbox


def _make_runner(ctx, *, compact_proxy=None, **config_kwargs):
    config = AgentSessionConfig(
        fold_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        per_call_timeout_seconds=60,
        **config_kwargs,
    )
    proxy = ScriptedLLM([])
    return AgentSessionRunner(
        ctx,
        proxy,
        config,
        fold_info={"fold_id": "fold_2022Q1"},
        acceptance_rules={"min_return": 0.0},
        compact_proxy=compact_proxy,
    )


class _StubBacktest:
    def __init__(self, result):
        self._result = result

    def run(self, mode="valid", replay_window=None):
        return dict(self._result)


class StepGateEligibilityTest(unittest.TestCase):
    """pipeline_design.md §5.1: probes and FAILED backtests do not trigger the
    step gate; only a successful complete validation holds the session."""

    def _gate_calls(self, result, args):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            calls = []
            ctx.extra["step_gate_hook"] = lambda index, summary: calls.append((index, summary)) or ""
            runner = _make_runner(ctx)
            runner.backtest = _StubBacktest(result)
            runner._do_backtest(args)
            return calls, runner

    def test_failed_backtest_does_not_gate(self):
        calls, runner = self._gate_calls(
            {"status": "error", "complete_validation": False, "error": "strategy crashed"}, {}
        )
        self.assertEqual(calls, [])
        self.assertEqual(runner._accepted_steps, 0)

    def test_probe_backtest_does_not_gate(self):
        calls, runner = self._gate_calls(
            {"status": "ok", "complete_validation": False, "runtime_representative": False},
            {"replay_window": 3},
        )
        self.assertEqual(calls, [])
        self.assertEqual(runner._accepted_steps, 0)

    def test_successful_complete_validation_gates_with_step_index(self):
        calls, runner = self._gate_calls(
            {"status": "ok", "complete_validation": True, "total_return": 0.01}, {}
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 1)
        self.assertEqual(runner._accepted_steps, 1)


class DeterministicTrimWithoutCompactorTest(unittest.TestCase):
    def _messages(self, count=10, chars=600):
        messages = [{"role": "system", "content": "system prompt"}]
        for index in range(count):
            messages.append({"role": "user", "content": f"m{index}:" + "x" * chars})
        return messages

    def test_token_trigger_under_message_cap_defers_to_compactor_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = _make_runner(ctx, compact_proxy=ScriptedLLM([]), trim_token_threshold=500)
            messages = self._messages()
            self.assertIs(runner._trim(messages), messages)

    def test_token_trigger_without_compactor_sheds_oldest_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, ctx = build_sandbox(Path(tmp))
            runner = _make_runner(ctx, compact_proxy=None, trim_token_threshold=500)
            messages = self._messages()
            trimmed = runner._trim(messages)
            self.assertLess(len(trimmed), len(messages))
            self.assertEqual(trimmed[0], messages[0])  # system survives
            rendered = "".join(str(m.get("content")) for m in trimmed)
            self.assertIn("m9:", rendered)  # newest raw message survives
            self.assertNotIn("m0:", rendered)  # oldest raw message dropped


class PromptExportRoundTripTest(unittest.TestCase):
    def test_prompts_md_matches_deterministic_generator_output(self):
        script = REPO_ROOT / "scripts" / "dev" / "export_prompts.py"
        spec = importlib.util.spec_from_file_location("_export_prompts", script)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
            first = module.render()
            second = module.render()
        finally:
            sys.modules.pop(spec.name, None)
        self.assertEqual(first, second, "prompt export must be byte-deterministic")
        committed = (REPO_ROOT / "configs" / "prompts" / "PROMPTS.md").read_text(encoding="utf-8")
        self.assertEqual(
            committed,
            first,
            "configs/prompts/PROMPTS.md is stale — regenerate with scripts/dev/export_prompts.py",
        )


if __name__ == "__main__":
    unittest.main()
