from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from hl_trader.agent.shadow import LLMShadowAdvisor, NLShadowRecorder
from hl_trader.agent.evidence import EvidencePack, EvidencePackBuilder, verify_pack_record
from hl_trader.environment.events import CheckpointDetector
from hl_trader.agent.llm import DeepSeekClient, DeepSeekConfig, load_deepseek_api_key
from hl_trader.environment.storage import TrialLedger
from hl_trader.environment.storage.ledger import stable_hash, to_jsonable


DEFAULT_SHADOW_FEATURE_COLUMNS = ("pe_ttm", "pb", "pct_chg", "amount", "amount_ma20", "ret_20d")
REQUIRED_SHADOW_PIT_COLUMNS = ("feature_date", "source_trade_date", "tradable_date", "available_at", "ts_code")
DEFAULT_EVIDENCE_OUT = Path("data/evidence_packs/llm_shadow.jsonl")
DEFAULT_SHADOW_LEDGER_PATH = Path("experiments/trial_ledger/llm_shadow.jsonl")


@dataclass(frozen=True)
class LLMShadowRunConfig:
    evidence_jsonl: Path | None
    shadow_ledger_path: Path
    evidence_out: Path | None = None
    model: str | None = None
    max_packs: int | None = None
    max_tokens: int = 1200
    dry_run: bool = False


@dataclass(frozen=True)
class LLMShadowRunResult:
    evidence_packs: int
    decisions: int
    checkpoints: int
    shadow_ledger_path: Path
    dry_run: bool


class LLMShadowPipeline:
    def __init__(
        self,
        advisor: LLMShadowAdvisor,
        *,
        recorder: NLShadowRecorder,
        run_ledger: TrialLedger | None = None,
    ) -> None:
        self.advisor = advisor
        self.recorder = recorder
        self.run_ledger = run_ledger

    @classmethod
    def from_deepseek_env(
        cls,
        *,
        shadow_ledger_path: str | Path,
        model: str | None = None,
        max_tokens: int = 1200,
        env_file: str | Path = ".env",
    ) -> LLMShadowPipeline:
        api_key = load_deepseek_api_key(env_file=env_file)
        config_kwargs: dict[str, Any] = {"api_key": api_key, "max_tokens": max_tokens}
        if model is not None:
            config_kwargs["model"] = model
        client = DeepSeekClient(DeepSeekConfig(**config_kwargs))
        advisor = LLMShadowAdvisor(client, provider_name="deepseek", max_tokens=max_tokens)
        return cls(advisor, recorder=NLShadowRecorder(shadow_ledger_path), run_ledger=TrialLedger(shadow_ledger_path))

    @classmethod
    def dry_run_only(cls, *, shadow_ledger_path: str | Path) -> LLMShadowPipeline:
        return cls(
            _DryRunAdvisor(),
            recorder=NLShadowRecorder(shadow_ledger_path),
            run_ledger=TrialLedger(shadow_ledger_path),
        )

    def run_records(
        self,
        evidence_records: list[dict[str, Any]],
        *,
        checkpoints_by_pack: dict[str, list[dict[str, Any]]] | None = None,
        dry_run: bool = False,
    ) -> LLMShadowRunResult:
        decisions = 0
        checkpoint_count = 0
        for record in evidence_records:
            verify_pack_record(record)
            pack_id = str(record.get("pack_id", ""))
            checkpoints = list((checkpoints_by_pack or {}).get(pack_id, []))
            checkpoint_count += len(checkpoints)
            if dry_run:
                self._record_dry_run(record, checkpoints)
                continue
            advice = self.advisor.advise(record, checkpoints=checkpoints)
            for decision in advice.decisions:
                self.recorder.append(
                    decision,
                    evidence_pack_id=pack_id,
                    provider_metadata=advice.provider_metadata,
                )
                decisions += 1
            if self.run_ledger:
                self.run_ledger.append({
                    "event_type": "llm_shadow_pack",
                    "evidence_pack_id": pack_id,
                    "prompt_hash": advice.prompt_hash,
                    "response_hash": advice.response_hash,
                    "decisions": len(advice.decisions),
                    "provider_metadata": advice.provider_metadata,
                    "can_affect_trading": False,
                })
        return LLMShadowRunResult(
            evidence_packs=len(evidence_records),
            decisions=decisions,
            checkpoints=checkpoint_count,
            shadow_ledger_path=self.recorder.ledger.path,
            dry_run=dry_run,
        )

    def _record_dry_run(self, record: dict[str, Any], checkpoints: list[dict[str, Any]]) -> None:
        if self.run_ledger:
            self.run_ledger.append({
                "event_type": "llm_shadow_dry_run",
                "evidence_pack_id": record.get("pack_id"),
                "evidence_hash": record.get("pack_hash"),
                "checkpoint_hash": stable_hash(checkpoints),
                "checkpoint_count": len(checkpoints),
                "ts_codes": record.get("ts_codes", []),
                "can_affect_trading": False,
            })


def build_evidence_pack_from_feature_file(
    feature_file: str | Path,
    *,
    decision_date: str,
    tradable_date: str,
    ts_codes: list[str],
    feature_columns: list[str] | None = None,
    evidence_out: str | Path | None = None,
) -> tuple[EvidencePack, list[dict[str, Any]]]:
    frame = _read_feature_file(feature_file)
    _validate_feature_file_frame(frame)
    columns = list(feature_columns or [col for col in DEFAULT_SHADOW_FEATURE_COLUMNS if col in frame.columns])
    if not columns:
        raise ValueError("no feature columns available for evidence pack")
    builder = EvidencePackBuilder(source_system="llm_shadow_pipeline")
    pack = builder.from_feature_cross_section(
        frame,
        decision_date=decision_date,
        tradable_date=tradable_date,
        ts_codes=ts_codes,
        feature_columns=columns,
    )
    if evidence_out is not None:
        builder.append_jsonl(evidence_out, pack)
    selected = frame[frame["ts_code"].astype(str).isin([str(code) for code in ts_codes])].copy()
    checkpoints = [to_jsonable(item) for item in CheckpointDetector().detect(selected)]
    return pack, checkpoints


def load_evidence_records(path: str | Path, *, max_packs: int | None = None) -> list[dict[str, Any]]:
    records = EvidencePackBuilder.read_jsonl(path)
    if max_packs is not None:
        if max_packs <= 0:
            raise ValueError("max_packs must be positive")
        records = records[:max_packs]
    return records


def _read_feature_file(path: str | Path) -> pd.DataFrame:
    input_path = Path(path)
    suffix = input_path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(input_path)
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(input_path)
    if suffix in {".json", ".jsonl"}:
        return pd.read_json(input_path, lines=suffix == ".jsonl")
    raise ValueError(f"unsupported feature file type: {input_path.suffix}")


def _validate_feature_file_frame(frame: pd.DataFrame) -> None:
    missing = set(REQUIRED_SHADOW_PIT_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"feature file missing PIT columns: {sorted(missing)}")


class _DryRunAdvisor:
    def advise(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("dry-run advisor must not be called")
