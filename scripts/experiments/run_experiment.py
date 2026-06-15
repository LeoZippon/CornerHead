#!/usr/bin/env python3
"""Experiment pipeline entrypoint (docs/pipeline_design.md).

Runs the development Fold/Epoch loop and the frozen held-out evaluation with
the real raw-data snapshot provider and the DeepSeek LLM proxy. The docs do
not prescribe a CLI; this thin wrapper only wires documented components.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _bootstrap import add_repo_src

add_repo_src(__file__)

from hl_trader.agent import AgentSessionConfig, AgentSessionRunner
from hl_trader.environment.llm import DeepSeekProxy
from hl_trader.environment.tools import ToolContext
from hl_trader.environment.web_search import SemanticScholarSearchProvider, TavilySearchProvider
from hl_trader.pipelines import ExperimentConfig, ExperimentPipeline, RawSnapshotProvider, load_sse_trading_days


DEFAULT_AGENT_MODEL = "deepseek-v4-pro"
DEFAULT_NL_MODEL = "deepseek-v4-flash"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the quarterly single-agent experiment pipeline.")
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--fundamental-events-root", type=Path, default=Path("data/features/fundamental_events"))
    parser.add_argument("--experiments-root", type=Path, default=Path("experiments"))
    parser.add_argument("--work-root", type=Path, default=Path(".runtime/sandboxes"))
    parser.add_argument("--template-dir", type=Path, default=Path("configs/agent_output_template"))
    parser.add_argument("--first-test-quarter", default="2022Q1")
    parser.add_argument("--last-test-quarter", default="2025Q4")
    parser.add_argument("--heldout-first-quarter", required=True)
    parser.add_argument("--heldout-last-quarter", required=True)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument(
        "--max-fold-minutes",
        type=int,
        default=30,
        help="Wall-clock deadline per Fold and meta-learning run.",
    )
    parser.add_argument(
        "--convergence-start-epoch",
        type=int,
        default=3,
        help="1-based Epoch index from which the Agent prompt enters convergence mode.",
    )
    parser.add_argument(
        "--disable-step-tree",
        action="store_true",
        help="Disable the cross-Fold Step artifact tree for ablation runs.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=10,
        help="Maximum factor-ranked candidates passed into full NL scoring.",
    )
    parser.add_argument(
        "--nl-failure-policy",
        choices=("fail", "neutral_with_audit"),
        default="neutral_with_audit",
        help="How formal NL scoring handles individual candidate failures.",
    )
    parser.add_argument("--model", default=DEFAULT_AGENT_MODEL, help="Agent main-conversation model.")
    parser.add_argument(
        "--nl-model",
        default=DEFAULT_NL_MODEL,
        help="NL scoring model; defaults to deepseek-v4-flash (independent interface).",
    )
    parser.add_argument("--local-dev", action="store_true", help="Use the local executor for development/tests only.")
    parser.add_argument("--no-thinking", action="store_true", help="Disable provider reasoning mode.")
    parser.add_argument(
        "--web-search-provider",
        choices=("tavily", "semantic_scholar", "disabled"),
        default="tavily",
        help="Provider for the Epoch-start meta-learning search tool.",
    )
    parser.add_argument("--tavily-api-key-env", default="TAVILY_API_KEY")
    parser.add_argument("--semantic-scholar-api-key-env", default="SEMANTIC_SCHOLAR_API_KEY")
    args = parser.parse_args()

    config = ExperimentConfig(
        experiment_id=args.experiment_id,
        experiments_root=args.experiments_root,
        work_root=args.work_root,
        template_dir=args.template_dir,
        first_test_quarter=args.first_test_quarter,
        last_test_quarter=args.last_test_quarter,
        heldout_first_quarter=args.heldout_first_quarter,
        heldout_last_quarter=args.heldout_last_quarter,
        epochs=args.epochs,
        max_fold_minutes=args.max_fold_minutes,
        max_candidates=args.max_candidates,
        nl_failure_policy=args.nl_failure_policy,
        convergence_start_epoch=args.convergence_start_epoch,
        step_tree_enabled=not args.disable_step_tree,
        use_docker=not args.local_dev,
    )
    proxy = DeepSeekProxy.from_env(model=args.model, thinking_enabled=not args.no_thinking)
    nl_proxy = proxy if args.nl_model == args.model else DeepSeekProxy.from_env(
        model=args.nl_model, thinking_enabled=not args.no_thinking
    )
    if args.web_search_provider == "tavily":
        web_search_provider = TavilySearchProvider.from_env(env_var=args.tavily_api_key_env)
    elif args.web_search_provider == "semantic_scholar":
        web_search_provider = SemanticScholarSearchProvider.from_env(env_var=args.semantic_scholar_api_key_env)
    else:
        web_search_provider = None

    def session_config(manifest_data: dict[str, object]) -> AgentSessionConfig:
        return AgentSessionConfig(
            fold_deadline_at=datetime.fromisoformat(str(manifest_data["fold_deadline_at"])),
            finalize_before_deadline_seconds=config.finalize_before_deadline_seconds,
            per_call_timeout_seconds=config.per_call_timeout_seconds,
            max_steps=config.max_steps_per_fold,
        )

    def agent_factory(ctx: ToolContext, fold, manifest_data: dict[str, object]) -> AgentSessionRunner:
        return AgentSessionRunner(
            ctx,
            proxy,
            session_config(manifest_data),
            fold_info=fold.to_record(),
            acceptance_rules=config.acceptance.to_record(),
            phase=str(manifest_data.get("phase", "exploration")),
            step_tree_enabled=bool(manifest_data.get("step_tree_enabled", False)),
            taste_prompt=str(manifest_data.get("taste_prompt", "")),
        )

    def meta_learner(ctx: ToolContext) -> None:
        AgentSessionRunner(
            ctx,
            proxy,
            session_config(ctx.manifest.data),
            fold_info=dict(ctx.manifest.get("development_inputs", {})),
            acceptance_rules={},
            mode="meta_learning",
            web_search_provider=web_search_provider,
        ).run()

    pipeline = ExperimentPipeline(
        config,
        RawSnapshotProvider(args.raw_dir, args.fundamental_events_root),
        agent_factory,
        proxy=proxy,
        nl_proxy=nl_proxy,
        meta_learner=meta_learner,
    )
    result = pipeline.run(load_sse_trading_days(args.raw_dir))
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
