"""Sandbox runtime files: paths, run_manifest.json, and agent_trace.jsonl.

Trusted logs are produced only by Runner / Execution Gateway / LLM Proxy /
simulated Broker code paths (docs/environment_design.md chapter 7). Agent text
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

SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
SENSITIVE_KEYS = {"api_key", "apikey", "authorization", "access_token", "token", "secret", "password"}

ARTIFACT_TOP_LEVEL = (
    "run_manifest.json",
    "agent_trace.jsonl",
    "parent_output",
    "results",
    "steps",
    "logs",
)
AGENT_TOP_LEVEL = ("workspace", "agent_output")


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
    def agent_trace(self) -> Path:
        return self.artifacts / "agent_trace.jsonl"

    @property
    def parent_output(self) -> Path:
        return self.artifacts / "parent_output"

    @property
    def workspace(self) -> Path:
        return self.agent / "workspace"

    @property
    def agent_output(self) -> Path:
        return self.agent / "agent_output"

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
        return SECRET_PATTERN.sub("sk-[redacted]", value)
    return value


@dataclass
class RunManifest:
    """The per-run manifest at /mnt/artifacts/run_manifest.json."""

    path: Path
    data: dict[str, object] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def create(cls, path: str | Path, initial: dict[str, object]) -> "RunManifest":
        manifest = cls(path=Path(path), data=dict(initial))
        manifest.data.setdefault("created_at", utc_now_iso())
        manifest.data.setdefault("backtest_summaries", [])
        manifest.save()
        return manifest

    @classmethod
    def load(cls, path: str | Path) -> "RunManifest":
        path = Path(path)
        return cls(path=path, data=json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(sanitize_for_log(self.data), ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def update(self, **fields: object) -> None:
        with self._lock:
            self.data.update(fields)
            self.save()

    def record_modification_check(self, summary: dict[str, object]) -> None:
        """Keep only the latest check summary (docs/environment_design.md 3.2)."""
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
