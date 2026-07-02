"""Sandbox runtime files: paths, run_manifest.json, and agent_trace.jsonl.

Trusted logs are produced only by Runner / Execution Gateway / LLM Proxy /
simulated Broker code paths (docs/environment_design.md §4.1). Agent text
never replaces these records.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from autotrade.environment.identity import agent_visible_ref as _agent_visible_ref

SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,;]+"), r"\1[redacted]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-[redacted]"),
    (re.compile(r"hf_[A-Za-z0-9]{8,}"), "hf_[redacted]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]+"), "github_pat_[redacted]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{8,}"), "gh_[redacted]"),
    (re.compile(r"vless:" + r"//[^\s'\"<>]+"), "vless:" + "//[redacted]"),
    (
        re.compile(r"\b((?:https?|socks5h?|socks4)://)[^/\s'\"<>:@]+:[^@\s'\"<>]+@"),
        r"\1[redacted]@",
    ),
)
SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "token",
    "secret",
    "password",
    "github_token",
    "hf_token",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "proxy",
    "proxy_url",
}

ARTIFACT_TOP_LEVEL = (
    "run_manifest.json",
    "runtime_env.json",
    "data_summary.json",
    "agent_trace.jsonl",
    "parent_output",
    "parent_models",
    "results",
    "steps",
    "logs",
)
AGENT_TOP_LEVEL = ("workspace", "output", "models")
# Python bytecode-cache dirs/suffixes that are never experiment artifacts. Single
# source for both the artifact-collection ignore list (sandbox._COLLECT_IGNORE, which
# adds VCS/venv/tooling dirs on top) and the formal-file runtime-cache predicate
# (artifacts._is_runtime_cache).
RUNTIME_CACHE_DIR_NAMES = ("__pycache__",)
RUNTIME_CACHE_SUFFIXES = (".pyc", ".pyo")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class SandboxPaths:
    """Resolved sandbox mount points.

    In Docker these are the fixed /mnt/... paths; the local driver maps them
    under a host directory with the same relative layout.
    """

    root: Path

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def train(self) -> Path:
        return self.snapshots / "train"

    @property
    def valid(self) -> Path:
        return self.snapshots / "valid"

    @property
    def test(self) -> Path:
        return self.snapshots / "test"

    @property
    def snapshot(self) -> Path:
        """Current formal decision-input view bound before backtest_tool runs."""
        return self.root / "snapshot"

    @property
    def snapshot_views(self) -> Path:
        return self.root / "runtime" / "snapshot_views"

    @property
    def current_snapshot(self) -> Path:
        """Host-side current decision-input mirror mounted as /mnt/snapshot."""
        return self.root / "runtime" / "current_snapshot"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def agent(self) -> Path:
        """Agent-writable mount root."""
        return self.root / "agent"

    @property
    def run_manifest(self) -> Path:
        return self.artifacts / "run_manifest.json"

    @property
    def host_run_manifest(self) -> Path:
        """Host-only full manifest used for audit; never mounted to Agent."""
        return self.root / "runtime" / "host_run_manifest.json"

    @property
    def runtime_env(self) -> Path:
        return self.artifacts / "runtime_env.json"

    @property
    def data_summary(self) -> Path:
        return self.artifacts / "data_summary.json"

    @property
    def agent_trace(self) -> Path:
        return self.artifacts / "agent_trace.jsonl"

    @property
    def parent_output(self) -> Path:
        return self.artifacts / "parent_output"

    @property
    def parent_model_artifacts(self) -> Path:
        return self.artifacts / "parent_models"

    @property
    def parent_models(self) -> Path:
        return self.parent_model_artifacts

    @property
    def workspace(self) -> Path:
        return self.agent / "workspace"

    @property
    def agent_output(self) -> Path:
        return self.agent / "output"

    @property
    def output(self) -> Path:
        """Agent formal strategy output directory.

        ``agent_output`` remains the internal API name for the strategy
        artifact concept; the sandbox-visible path is /mnt/agent/output.
        """
        return self.agent_output

    @property
    def model_artifacts(self) -> Path:
        """Agent model-parameter artifact directory.

        Strategy code lives in ``output``. Optional trained parameters and
        weights live here and are hashed/frozen separately.
        """
        return self.agent / "models"

    @property
    def models(self) -> Path:
        return self.model_artifacts

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        """The three sandbox roots the agent may write to (single source of truth
        for the shell write guard and the artifact_io tools)."""
        return (self.workspace, self.agent_output, self.model_artifacts)

    @property
    def writable_root_map(self) -> dict[str, Path]:
        """Agent-facing writable-root name (see ``AGENT_TOP_LEVEL``) -> path."""
        return {"workspace": self.workspace, "output": self.agent_output, "models": self.model_artifacts}

    @property
    def results(self) -> Path:
        return self.artifacts / "results"

    @property
    def steps(self) -> Path:
        """Step artifact tree (lineage of validated Step artifacts)."""
        return self.artifacts / "steps"

    @property
    def logs(self) -> Path:
        return self.artifacts / "logs"


def sanitize_for_log(value: object) -> object:
    """Drop sensitive keys and redact secret-looking strings recursively."""
    if isinstance(value, dict):
        return {
            key: "[redacted]" if str(key).lower() in SENSITIVE_KEYS else sanitize_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, str):
        text = value
        for pattern, replacement in SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
    return value


@dataclass
class RunManifest:
    """Per-run manifest with an Agent-visible public view and host audit view."""

    path: Path
    data: dict[str, object] = field(default_factory=dict)
    host_path: Path | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def create(cls, path: str | Path, initial: dict[str, object]) -> "RunManifest":
        path = Path(path)
        manifest = cls(path=path, host_path=_default_host_manifest_path(path), data=dict(initial))
        manifest.data.setdefault("created_at", utc_now_iso())
        manifest.data.setdefault("backtest_summaries", [])
        manifest.save()
        return manifest

    @classmethod
    def load(cls, path: str | Path) -> "RunManifest":
        path = Path(path)
        return cls(path=path, data=json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        if self.host_path is not None:
            _write_json_atomic(self.host_path, sanitize_for_log(self.data))
        _write_json_atomic(self.path, _agent_visible_manifest(self.data))

    def update(self, **fields: object) -> None:
        with self._lock:
            self.data.update(fields)
            self.save()

    def record_modification_check(self, summary: dict[str, object]) -> None:
        """Keep only the latest check summary (docs/environment_design.md §2.3)."""
        self.update(last_modification_check=summary)

    def append_backtest_summary(self, summary: dict[str, object]) -> None:
        with self._lock:
            summaries = list(self.data.get("backtest_summaries", []))
            summaries.append(summary)
            self.data["backtest_summaries"] = summaries
            self.save()

    def get(self, key: str, default: object = None) -> object:
        return self.data.get(key, default)

    def require(self, key: str) -> object:
        if key not in self.data:
            raise KeyError(f"run manifest missing required key: {key}")
        return self.data[key]


def _default_host_manifest_path(public_path: Path) -> Path:
    if public_path.parent.name == "artifacts":
        return public_path.parent.parent / "runtime" / "host_run_manifest.json"
    return public_path.with_name("host_run_manifest.json")


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)


def _agent_visible_manifest(data: dict[str, object]) -> dict[str, object]:
    """Return the public manifest view mounted at /mnt/artifacts.

    The in-memory and host audit manifest keep the full schedule and frozen
    test details for orchestration. Agent-visible files only carry training /
    validation facts so meta learning cannot accidentally consume test feedback.
    """

    record = json.loads(json.dumps(sanitize_for_log(data), ensure_ascii=False, default=str))
    if not isinstance(record, dict):
        return {}
    public: dict[str, object] = {
        key: record[key]
        for key in (
            "experiment_id",
            "epoch_id",
            "run_id",
            "conversation_id",
            "kind",
            "runtime_env_ref",
            "data_summary_ref",
            "fold_period",
            "snapshot_config",
            "valid_decision_time",
            "is_initial_artifact",
            "parent_strategy_artifact_id",
            "parent_strategy_artifact_hash",
            "parent_model_artifact_hash",
            "template_ref",
            "initial_template_hash",
            "modification_constraints",
            "acceptance_rules",
            "broker_profile",
            "short_inventory_mode",
            "nl_failure_policy",
            "step_tree_enabled",
            "record_failed_attempts",
            "epoch_index",
            "phase",
            "max_steps",
            "max_backtests_per_fold",
            "fold_deadline_at",
            "finalize_before_deadline_seconds",
            "per_call_timeout_seconds",
            "execution_lag_bars",
            "decision_max_sim_minutes",
            "backtest_max_seconds_per_decision",
            "backtest_max_seconds_per_trading_day",
            "auction_enabled",
            "auction_preopen_time",
            "auction_decision_time",
            "auction_close_time",
            "offsession_tick_minutes",
            "timeview_enabled",
            "rolling_asof_enabled",
            "nl_max_calls_per_decision_day",
            "nl_max_calls_per_backtest",
            "sandbox_spec",
            "sandbox_runtime",
            "sandbox_image_update",
            "taste_prompt",
            "development_inputs",
            "taste_output",
            "meta_learning_directive",
            "web_search_engines",
            "created_at",
            "frozen_strategy_artifact_hash",
            "frozen_model_artifact_hash",
        )
        if key in record
    }
    if "fold_id" in record:
        public["fold_id"] = _agent_visible_ref(record.get("fold_id"), prefix="fold_ref")
    # Artifact ids embed the raw fold label (strategy_<epoch>_fold_<period>), so they
    # must be projected exactly like the ledger view does.
    if public.get("parent_strategy_artifact_id"):
        public["parent_strategy_artifact_id"] = _agent_visible_ref(
            public["parent_strategy_artifact_id"], prefix="strategy_ref"
        )
    if isinstance(record.get("fold"), dict):
        public["fold"] = _agent_visible_fold_record(record["fold"])
    if isinstance(record.get("meta_learning_visible_fold"), dict):
        public["meta_learning_visible_fold"] = _agent_visible_fold_record(
            record["meta_learning_visible_fold"]
        )
    if isinstance(record.get("snapshots"), dict):
        public["snapshots"] = _agent_visible_snapshots(record["snapshots"])
    if isinstance(record.get("experiment_parameters"), dict):
        public["experiment_parameters"] = _agent_visible_experiment_parameters(
            record["experiment_parameters"]
        )
    if isinstance(record.get("backtest_summaries"), list):
        public["backtest_summaries"] = [
            _agent_visible_backtest_summary(item)
            for item in record["backtest_summaries"]
            if isinstance(item, dict) and item.get("mode") == "valid"
        ]
    return public


def _agent_visible_fold_record(record: dict[str, object]) -> dict[str, object]:
    public = {
        key: record[key]
        for key in ("input_window", "validation_period", "valid_decision_time")
        if key in record
    }
    if "fold_id" in record:
        public["fold_id"] = _agent_visible_ref(record.get("fold_id"), prefix="fold_ref")
    return public


def _agent_visible_snapshots(record: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"test_decision_input", "test_replay", "heldout_decision_input", "heldout_replay"}
        and not str(key).startswith("test_")
        and not str(key).startswith("heldout_")
    }


def _agent_visible_experiment_parameters(record: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in record.items()
        if key != "periods" and not str(key).startswith("heldout_")
    }


def _agent_visible_backtest_summary(record: dict[str, object]) -> dict[str, object]:
    return {
        key: record[key]
        for key in (
            "result_name",
            "mode",
            "status",
            "complete_validation",
            "total_return",
            "long_return",
            "short_return",
            "sharpe",
            "max_drawdown",
            "margin_secs_reject_count",
            "order_count",
            "model_artifact_files",
            "model_artifact_bytes",
            "artifact_hash",
            "model_artifact_hash",
            "combined_artifact_hash",
            "result_path",
            "started_at",
            "finished_at",
            "replay_wall_seconds",
            "replayed_trade_days",
            "substep_runtime",
            "phase_seconds",
            "total_ticks",
            "intraday_ticks",
            "offsession_ticks",
            "state_staged_writes",
            "state_unmerged_writes",
            "error",
            "modification_delta_summary",
        )
        if key in record
    }


class AgentTraceWriter:
    """Append-only event stream for one Agent session / conversation trace."""

    def __init__(self, path: str | Path, *, ids: dict[str, str]) -> None:
        self.path = Path(path)
        self.ids = dict(ids)
        self._lock = threading.Lock()

    def emit(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        step_id: str | None = None,
        parent_call_id: str | None = None,
        phase: str | None = None,
    ) -> str:
        call_id = new_id("call")
        record: dict[str, object] = {
            "event_type": event_type,
            "ts": utc_now_iso(),
            "call_id": call_id,
            "parent_call_id": parent_call_id,
            **self.ids,
        }
        if step_id is not None:
            record["step_id"] = step_id
        if phase is not None:
            # Tags events the host pipeline emits outside the agent loop (e.g. the
            # post-session modification check) so audits don't read them as agent
            # actions; agent-driven events leave this unset.
            record["phase"] = phase
        record.update(sanitize_for_log(payload))
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return call_id

    def read_events(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
