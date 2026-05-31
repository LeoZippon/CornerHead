#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from hl_trader.environment.features import DailyPITFeatureBuilder, FeatureBuildConfig
from hl_trader.pipelines import DailyFormulaicExperimentRunner, DailyFormulaicHeldoutRunner, read_feature_frame
from hl_trader.pipelines.llm_shadow import (
    DEFAULT_EVIDENCE_OUT,
    DEFAULT_SHADOW_LEDGER_PATH,
    LLMShadowPipeline,
    build_evidence_pack_from_feature_file,
    load_evidence_records,
)
from hl_trader.environment.schemas import load_experiment_config
from hl_trader.environment.storage.ledger import to_jsonable
from hl_trader.agent import FormulaicParameters


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, sort_keys=True, default=to_jsonable))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HL research command entrypoint.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-features", help="build next-day tradable daily PIT features")
    build.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    build.add_argument("--output-root", type=Path, default=Path("data/features"))
    build.add_argument("--dataset", default="daily_alpha")
    build.add_argument("--start-date", required=True, help="YYYYMMDD or ISO date.")
    build.add_argument("--end-date", required=True, help="YYYYMMDD or ISO date.")
    build.add_argument("--lookback-days", type=int, default=80)
    build.add_argument("--no-limit-list", action="store_true", help="Do not join optional limit_list_d events.")
    build.set_defaults(handler=run_build_features)

    development = sub.add_parser("run-development", help="run frozen formulaic development WFO")
    add_experiment_common_args(development)
    development.add_argument("--max-folds", type=int, help="Optional cap for smoke runs.")
    development.set_defaults(handler=run_development)

    heldout = sub.add_parser("run-heldout", help="run frozen formulaic held-out evaluation without fitting")
    add_experiment_common_args(heldout)
    heldout.add_argument("--top-n", type=int, required=True)
    heldout.add_argument("--max-pe-ttm-quantile", type=float, required=True)
    heldout.add_argument("--max-pb-quantile", type=float, required=True)
    heldout.add_argument("--min-amount-quantile", type=float, required=True)
    heldout.add_argument("--treatment", default="control_formulaic")
    heldout.set_defaults(handler=run_heldout)

    shadow = sub.add_parser("llm-shadow", help="run LLM NL shadow decisions from PIT evidence packs")
    source = shadow.add_mutually_exclusive_group(required=True)
    source.add_argument("--evidence-jsonl", type=Path, help="Existing evidence pack JSONL.")
    source.add_argument("--feature-file", type=Path, help="Feature cross-section file: parquet/csv/json/jsonl.")
    shadow.add_argument("--decision-date", help="YYYYMMDD decision date when --feature-file is used.")
    shadow.add_argument("--tradable-date", help="YYYYMMDD tradable date when --feature-file is used.")
    shadow.add_argument("--ts-code", action="append", default=[], help="Stock code to include; repeatable.")
    shadow.add_argument("--feature-column", action="append", default=[], help="Feature column to include; repeatable.")
    shadow.add_argument("--evidence-out", type=Path, default=DEFAULT_EVIDENCE_OUT)
    shadow.add_argument("--shadow-ledger", type=Path, default=DEFAULT_SHADOW_LEDGER_PATH)
    shadow.add_argument("--provider", choices=["deepseek"], default="deepseek")
    shadow.add_argument("--model", help="Provider model override; defaults to the provider adapter default.")
    shadow.add_argument("--max-packs", type=int)
    shadow.add_argument("--max-tokens", type=int, default=1200)
    shadow.add_argument("--dry-run", action="store_true", help="Build/validate evidence and ledger dry-run without API call.")
    shadow.set_defaults(handler=run_llm_shadow)
    return parser


def add_experiment_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True, help="Experiment YAML config.")
    parser.add_argument("--features", type=Path, required=True, help="Feature file or partition directory.")
    parser.add_argument("--ledger-path", type=Path, help="Override ledger path from the YAML config for this run.")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--model-id", default="formulaic")
    parser.add_argument("--prompt-id", default="")
    parser.add_argument("--data-contract-id", default="tushare_daily_pit_v1")


def run_build_features(args: argparse.Namespace) -> dict[str, object]:
    builder = DailyPITFeatureBuilder(args.raw_dir)
    features = builder.build(FeatureBuildConfig(
        start_date=args.start_date,
        end_date=args.end_date,
        lookback_days=args.lookback_days,
        output_dataset=args.dataset,
        include_limit_list=not args.no_limit_list,
    ))
    written = builder.write_partitioned(features, args.output_root, dataset=args.dataset)
    return {
        "rows": int(len(features)),
        "partitions": len(written),
        "output_dir": str(args.output_root / args.dataset),
        "first_partition": str(written[0]) if written else None,
        "last_partition": str(written[-1]) if written else None,
    }


def run_development(args: argparse.Namespace) -> dict[str, object]:
    features = read_feature_frame(args.features)
    runner = DailyFormulaicExperimentRunner(
        load_experiment_config_with_overrides(args),
        model_id=args.model_id,
        prompt_id=args.prompt_id,
        data_contract_id=args.data_contract_id,
    )
    result = runner.run(features, max_folds=args.max_folds, initial_cash=args.initial_cash)
    return {"result": result}


def run_heldout(args: argparse.Namespace) -> dict[str, object]:
    features = read_feature_frame(args.features)
    params = FormulaicParameters(
        top_n=args.top_n,
        max_pe_ttm_quantile=args.max_pe_ttm_quantile,
        max_pb_quantile=args.max_pb_quantile,
        min_amount_quantile=args.min_amount_quantile,
    )
    runner = DailyFormulaicHeldoutRunner(
        load_experiment_config_with_overrides(args),
        frozen_parameters=params,
        treatment=args.treatment,
        model_id=args.model_id,
        prompt_id=args.prompt_id,
        data_contract_id=args.data_contract_id,
    )
    return {"result": runner.run(features, initial_cash=args.initial_cash)}


def load_experiment_config_with_overrides(args: argparse.Namespace):
    config = load_experiment_config(args.config)
    if args.ledger_path is None:
        return config
    return replace(config, ledger_path=args.ledger_path.resolve())


def run_llm_shadow(args: argparse.Namespace) -> dict[str, object]:
    if args.feature_file:
        if not args.decision_date or not args.tradable_date or not args.ts_code:
            raise ValueError("--feature-file requires --decision-date, --tradable-date, and at least one --ts-code")
        pack, checkpoints = build_evidence_pack_from_feature_file(
            args.feature_file,
            decision_date=args.decision_date,
            tradable_date=args.tradable_date,
            ts_codes=args.ts_code,
            feature_columns=args.feature_column or None,
            evidence_out=args.evidence_out,
        )
        evidence_records = [pack.to_record()]
        checkpoints_by_pack = {pack.pack_id: checkpoints}
    else:
        evidence_records = load_evidence_records(args.evidence_jsonl, max_packs=args.max_packs)
        checkpoints_by_pack = {}

    if args.dry_run:
        pipeline = LLMShadowPipeline.dry_run_only(shadow_ledger_path=args.shadow_ledger)
    else:
        if args.provider != "deepseek":
            raise ValueError(f"unsupported provider={args.provider}")
        pipeline = LLMShadowPipeline.from_deepseek_env(
            shadow_ledger_path=args.shadow_ledger,
            model=args.model,
            max_tokens=args.max_tokens,
        )
    result = pipeline.run_records(evidence_records, checkpoints_by_pack=checkpoints_by_pack, dry_run=args.dry_run)
    return {"result": result}


if __name__ == "__main__":
    raise SystemExit(main())
