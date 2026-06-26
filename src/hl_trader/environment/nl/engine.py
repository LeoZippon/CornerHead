"""Host-side NL Sub Agent with point-in-time text retrieval.

``mq_tools.nl(...)`` starts one bounded host-side Sub Agent task. The Sub Agent
may call the ``text_retrieve`` tool, which is backed by the snapshot
``text_index.parquet`` and ``text_library/``. The final answer is intentionally
free-form: strategy code receives the Sub Agent result and decides how to parse
or use it.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from hl_trader.environment.llm.proxy import LLMProxy, LLMProxyError, ProviderResponse
from hl_trader.environment.runtime import new_id, sanitize_for_log, utc_now_iso

TERMINAL_STATES = ("completed", "failed_with_policy", "timeout", "failed")
MAX_TOOL_ROUNDS = 3
TEXT_RETRIEVE_TOOL = "text_retrieve"

TEXT_RETRIEVE_SCHEMA = {
    "type": "function",
    "function": {
        "name": TEXT_RETRIEVE_TOOL,
        "description": (
            "Retrieve point-in-time text evidence by case-insensitive grep/regex over titles, "
            "codes, and optional full text bodies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "case-insensitive grep/regex pattern"},
                "ts_code": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                "search_bodies": {"type": "boolean"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
}

SUB_AGENT_SYSTEM_PROMPT = """\
# Role
You are an A-share point-in-time natural-language research Sub Agent. You help
strategy code answer the user's prompt for one stock or decision context.

# Data Boundary
Use only the context and text evidence returned by tools in this task. Do not
use future events, price moves after the decision time, private credentials, or
unstated facts from memory.

# Available Tool
Call the ``text_retrieve`` function tool (native function calling) to fetch text
evidence. ``pattern`` uses case-insensitive grep/regex semantics over titles,
codes, and optional full text bodies; prefer company/code/business-context
patterns before broad market patterns. Optional arguments: ``ts_code``,
``max_results`` (1-20), ``search_bodies``.

# Final Answer
When you have enough information, answer in any format that is useful to the
calling strategy: plain text, JSON, bullet points, a numeric rubric, or a short
decision note are all allowed. Do not fabricate evidence identifiers.
"""

FINAL_AFTER_TOOL_BUDGET = (
    "The text retrieval budget for this NL Sub Agent task is exhausted. "
    "Return your final answer now in any format. Do not request more tools."
)


@dataclass(frozen=True)
class NLSubAgentConfig:
    per_call_timeout_seconds: float = 300.0
    max_tokens: int = 3000
    max_tool_rounds: int = MAX_TOOL_ROUNDS
    # ``fail`` makes the caller fail the backtest. ``return_error_with_audit``
    # returns an auditable result dict with status=error so Agent code can
    # decide how to handle unavailable text analysis.
    failure_policy: str = "fail"

    def __post_init__(self) -> None:
        if self.max_tool_rounds < 0:
            raise ValueError("max_tool_rounds must be non-negative")
        if self.failure_policy not in {"fail", "return_error_with_audit"}:
            raise ValueError(f"unsupported failure_policy={self.failure_policy}")


@dataclass
class NLSubAgentResult:
    ts_code: str
    task_id: str
    state: str
    content: str = ""
    error: str = ""
    rounds: int = 0
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    evidence: list[dict[str, object]] = field(default_factory=list)
    llm_calls: list[dict[str, object]] = field(default_factory=list)
    company_context: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == "completed"

    def to_record(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "ts_code": self.ts_code,
            "status": "ok" if self.ok else "error",
            "state": self.state,
            "content": self.content,
            "error": self.error,
            "rounds": self.rounds,
            "tool_calls": list(self.tool_calls),
            "evidence": list(self.evidence),
            "company_context": dict(self.company_context),
        }


class TextRetriever:
    """Grep-style retrieval over the snapshot text index and as-of text library.

    The NL Sub Agent supplies a regex pattern (case-insensitive grep semantics).
    Titles/codes are matched first, then full bodies when more results are
    needed; results rank candidate-related hits before broad background hits,
    recency second. Bodies live in per-dataset parquet shards under
    ``text_library/`` and are loaded lazily.
    """

    def __init__(self, text_index_path: str | Path, text_library_dir: str | Path, *, snippet_chars: int = 4000) -> None:
        self.text_library_dir = Path(text_library_dir)
        self.snippet_chars = snippet_chars
        path = Path(text_index_path)
        self.index = pd.read_parquet(path) if path.exists() else pd.DataFrame()
        self._bodies: dict[str, dict[str, str]] = {}
        self._body_series: dict[str, pd.Series] = {}
        self._load_lock = threading.Lock()

    def search(
        self,
        pattern: str,
        *,
        ts_code: str,
        max_results: int = 5,
        search_bodies: bool = True,
        company_terms: list[str] | None = None,
    ) -> list[dict[str, object]]:
        if self.index.empty:
            return []
        regex = _safe_regex(pattern)
        titles = self.index.get("title", pd.Series("", index=self.index.index)).astype(str)
        codes = self.index.get("ts_codes", pd.Series("", index=self.index.index)).astype(str)
        pattern_hit = titles.str.contains(regex, case=False, regex=True, na=False) | codes.str.contains(
            regex, case=False, regex=True, na=False
        )
        own_hit = self._candidate_mask(self.index, ts_code=ts_code, company_terms=company_terms)
        hits = self.index[pattern_hit].copy()
        hits["_relevance"] = "background"
        hits["_rank"] = 20
        hits.loc[own_hit[own_hit].index.intersection(hits.index), "_rank"] = 40
        hits.loc[own_hit[own_hit].index.intersection(hits.index), "_relevance"] = "candidate"
        if search_bodies and len(hits) < max_results:
            body_idx = self._grep_bodies(regex, exclude=set(hits["text_id"].astype(str)), limit=max_results * 3)
            if body_idx:
                body_rows = self.index[self.index["text_id"].astype(str).isin(body_idx)].copy()
                body_own = self._candidate_mask(body_rows, ts_code=ts_code, company_terms=company_terms)
                for idx, row in body_rows.loc[~body_own].iterrows():
                    if self._body_has_candidate_term(
                        str(row.get("dataset", "")),
                        str(row.get("text_id", "")),
                        ts_code=ts_code,
                        company_terms=company_terms,
                    ):
                        body_own.loc[idx] = True
                body_rows["_relevance"] = "background"
                body_rows["_rank"] = 10
                body_rows.loc[body_own[body_own].index.intersection(body_rows.index), "_rank"] = 30
                body_rows.loc[body_own[body_own].index.intersection(body_rows.index), "_relevance"] = "candidate"
                hits = pd.concat([hits, body_rows], ignore_index=False)
        if hits.empty:
            return []
        hits = hits.drop_duplicates(subset=["text_id"], keep="first")
        sort_cols = ["_rank"] + (["available_at"] if "available_at" in hits.columns else [])
        hits = hits.sort_values(sort_cols, ascending=[False] * len(sort_cols))
        records = []
        for row in hits.head(max_results).to_dict("records"):
            records.append(
                {
                    "text_id": str(row.get("text_id", "")),
                    "title": str(row.get("title", "")),
                    "available_at": str(row.get("available_at", "")),
                    "source_hash": str(row.get("source_hash", "")),
                    "ts_codes": str(row.get("ts_codes", "")),
                    "relevance": str(row.get("_relevance", "background")),
                    "snippet": self._snippet(str(row.get("dataset", "")), str(row.get("text_id", ""))),
                }
            )
        return records

    def _candidate_mask(
        self, frame: pd.DataFrame, *, ts_code: str, company_terms: list[str] | None = None
    ) -> pd.Series:
        codes = frame.get("ts_codes", pd.Series("", index=frame.index)).astype(str)
        titles = frame.get("title", pd.Series("", index=frame.index)).astype(str)
        mask = codes.str.contains(str(ts_code), case=False, regex=False, na=False)
        for term in _candidate_terms(ts_code, company_terms):
            escaped = re.escape(term)
            mask = mask | titles.str.contains(escaped, case=False, regex=True, na=False)
        return mask

    def _body_has_candidate_term(
        self, dataset: str, text_id: str, *, ts_code: str, company_terms: list[str] | None = None
    ) -> bool:
        body = self._snippet(dataset, text_id)
        if not body:
            return False
        lowered = body.lower()
        return any(term.lower() in lowered for term in _candidate_terms(ts_code, company_terms))

    def _grep_bodies(self, regex: str, *, exclude: set[str], limit: int) -> set[str]:
        found: set[str] = set()
        datasets = self.index.get("dataset")
        if datasets is None:
            return found
        for dataset in datasets.astype(str).unique():
            series = self._body_series_for(dataset)
            if series is None or series.empty:
                continue
            matched = series[series.str.contains(regex, case=False, regex=True, na=False)]
            found.update(tid for tid in matched.index.astype(str) if tid not in exclude)
            if len(found) >= limit:
                break
        return found

    def _body_series_for(self, dataset: str) -> pd.Series | None:
        with self._load_lock:
            if dataset not in self._body_series:
                shard = self.text_library_dir / f"{dataset}.parquet"
                if not shard.exists():
                    self._body_series[dataset] = pd.Series(dtype=str)
                    self._bodies[dataset] = {}
                else:
                    frame = pd.read_parquet(shard)
                    series = pd.Series(frame["body"].astype(str).values, index=frame["text_id"].astype(str))
                    self._body_series[dataset] = series
                    self._bodies[dataset] = series.to_dict()
            return self._body_series[dataset]

    def _snippet(self, dataset: str, text_id: str) -> str:
        if not dataset or not text_id:
            return ""
        self._body_series_for(dataset)
        return self._bodies.get(dataset, {}).get(text_id, "")[: self.snippet_chars]


class TextRetrieveTool:
    """Bounded tool facade exposed to the NL Sub Agent only."""

    def __init__(self, retriever: TextRetriever) -> None:
        self.retriever = retriever

    def call(
        self,
        arguments: dict[str, object],
        *,
        default_ts_code: str,
        company_terms: list[str],
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        pattern = _request_pattern(arguments)
        ts_code = str(arguments.get("ts_code") or default_ts_code)
        max_results = _bounded_int(arguments.get("max_results"), default=5, lower=1, upper=20)
        search_bodies = bool(arguments.get("search_bodies", True))
        argument_error = _text_retrieve_argument_error(arguments, pattern)
        if argument_error:
            return (
                {
                    "name": TEXT_RETRIEVE_TOOL,
                    "arguments": {
                        "pattern": pattern,
                        "ts_code": ts_code,
                        "max_results": max_results,
                        "search_bodies": search_bodies,
                    },
                    "status": "error",
                    "error": argument_error,
                    "hits": 0,
                    "result_ids": [],
                },
                [],
            )
        evidence = self.retriever.search(
            pattern,
            ts_code=ts_code,
            max_results=max_results,
            search_bodies=search_bodies,
            company_terms=company_terms,
        )
        record = {
            "name": TEXT_RETRIEVE_TOOL,
            "arguments": {
                "pattern": pattern,
                "ts_code": ts_code,
                "max_results": max_results,
                "search_bodies": search_bodies,
            },
            "hits": len(evidence),
            "result_ids": [item.get("text_id") for item in evidence],
        }
        return record, evidence


class NLSubAgentEngine:
    def __init__(
        self,
        proxy: LLMProxy,
        retriever: TextRetriever,
        *,
        company_contexts: dict[str, dict[str, object]],
    ) -> None:
        self.proxy = proxy
        self.retriever = retriever
        self.company_contexts = company_contexts
        self.text_tool = TextRetrieveTool(retriever)

    def run(
        self,
        *,
        ts_code: str,
        prompt: str,
        request_kwargs: dict[str, object] | None = None,
        config: NLSubAgentConfig,
    ) -> NLSubAgentResult:
        task = NLSubAgentResult(ts_code=ts_code, task_id=new_id("nlsub"), state="failed")
        task.company_context = self.company_contexts.get(ts_code, {"ts_code": ts_code, "context": "unknown"})
        messages = self._initial_messages(task, prompt=prompt, request_kwargs=request_kwargs or {})
        company_terms = _company_terms(task.company_context, ts_code)
        evidence_seen: set[str] = set()
        try:
            for round_index in range(1, config.max_tool_rounds + 1):
                task.rounds = round_index
                response = self._call(task, messages, config, purpose=f"subagent_round_{round_index}")
                calls = _parse_native_tool_calls(response.tool_calls)
                if not calls:
                    task.content = response.content
                    task.state = "completed"
                    return task
                messages.append(
                    {"role": "assistant", "content": response.content or "", "tool_calls": list(response.tool_calls)}
                )
                for tool_name, tool_call_id, arguments, call_error in calls:
                    new_evidence = []
                    if call_error:
                        tool_record = {
                            "name": tool_name,
                            "arguments": arguments,
                            "status": "error",
                            "error": call_error,
                            "round": round_index,
                        }
                    else:
                        tool_record, evidence = self.text_tool.call(
                            arguments, default_ts_code=ts_code, company_terms=company_terms
                        )
                        tool_record["round"] = round_index
                        for item in evidence:
                            text_id = str(item.get("text_id", ""))
                            if text_id and text_id not in evidence_seen:
                                evidence_seen.add(text_id)
                                task.evidence.append(item)
                            new_evidence.append(item)
                    task.tool_calls.append(tool_record)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": json.dumps(
                                {"tool_call": tool_record, "results": new_evidence}, ensure_ascii=False, sort_keys=True
                            ),
                        }
                    )
            task.rounds = max(task.rounds, config.max_tool_rounds)
            messages.append({"role": "user", "content": FINAL_AFTER_TOOL_BUDGET})
            # tool_choice="none" forces a final text answer instead of another tool call.
            response = self._call(
                task, messages, config, purpose="subagent_final_after_tool_budget", tool_choice="none"
            )
            task.content = response.content
            task.state = "completed"
        except LLMProxyError as exc:
            task.state = "timeout" if exc.timeout else self._failure_state(config)
            task.error = str(sanitize_for_log(str(exc)))
        except Exception as exc:  # noqa: BLE001 - convert Sub Agent failure into audited result
            task.state = self._failure_state(config)
            task.error = str(sanitize_for_log(str(exc)))
        return task

    def _call(
        self,
        task: NLSubAgentResult,
        messages: list[dict[str, object]],
        config: NLSubAgentConfig,
        *,
        purpose: str,
        tool_choice: str = "auto",
    ) -> ProviderResponse:
        detail: dict[str, object] = {
            "task_id": task.task_id,
            "ts_code": task.ts_code,
            "purpose": purpose,
            "started_at": utc_now_iso(),
            "messages": sanitize_for_log(messages),
            "provider": self.proxy.provider,
            "model": self.proxy.model,
        }
        try:
            response = self.proxy.complete_tools(
                messages,
                tools=[TEXT_RETRIEVE_SCHEMA],
                tool_choice=tool_choice,
                timeout_seconds=config.per_call_timeout_seconds,
                max_tokens=config.max_tokens,
            )
        except Exception as exc:
            detail.update(status="error", error=sanitize_for_log(str(exc)), completed_at=utc_now_iso())
            task.llm_calls.append(detail)
            raise
        detail.update(
            status="ok",
            completed_at=utc_now_iso(),
            content=response.content,
            reasoning_content=response.reasoning_content,
            tool_calls=sanitize_for_log([dict(tc) for tc in response.tool_calls]),
            usage=response.usage,
        )
        task.llm_calls.append(detail)
        return response

    def _initial_messages(
        self,
        task: NLSubAgentResult,
        *,
        prompt: str,
        request_kwargs: dict[str, object],
    ) -> list[dict[str, str]]:
        body = {
            "request": {
                "ts_code": task.ts_code,
                "prompt": prompt,
                "kwargs": request_kwargs,
            },
            "company_context": task.company_context,
        }
        return [
            {"role": "system", "content": SUB_AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(body, ensure_ascii=False, sort_keys=True)},
        ]

    @staticmethod
    def _failure_state(config: NLSubAgentConfig) -> str:
        return "failed_with_policy" if config.failure_policy == "return_error_with_audit" else "failed"


def _parse_native_tool_calls(tool_calls: object) -> list[tuple[str, str, dict[str, object], str]]:
    """Pull native ``text_retrieve`` calls and keep malformed arguments auditable."""
    parsed: list[tuple[str, str, dict[str, object], str]] = []
    for tool_call in tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            continue
        tool_name = str(function.get("name", "") or "")
        tool_call_id = str(tool_call.get("id") or new_id("call"))
        if tool_name != TEXT_RETRIEVE_TOOL:
            parsed.append(
                (
                    tool_name or "unknown",
                    tool_call_id,
                    {},
                    f"unsupported NL tool call: {tool_name or 'unknown'}; available tool is {TEXT_RETRIEVE_TOOL}",
                )
            )
            continue
        raw_arguments = function.get("arguments")
        error = ""
        if isinstance(raw_arguments, dict):
            arguments: dict[str, object] = dict(raw_arguments)
        elif isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                decoded = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                decoded = {}
                error = f"invalid text_retrieve arguments JSON: {exc.msg}"
            if isinstance(decoded, dict):
                arguments = decoded
            else:
                arguments = {}
                error = "text_retrieve arguments must be a JSON object"
        else:
            arguments = {}
        parsed.append((TEXT_RETRIEVE_TOOL, tool_call_id, arguments, error))
    return parsed


def _request_pattern(request: object) -> str:
    """Pattern from a search request; legacy keyword lists become alternations."""
    if not isinstance(request, dict):
        return ""
    pattern = str(request.get("pattern", "") or "").strip()
    if pattern:
        return pattern
    keywords = [str(k).strip() for k in request.get("keywords", []) if str(k).strip()]
    return "|".join(re.escape(keyword) for keyword in keywords)


def _text_retrieve_argument_error(arguments: dict[str, object], pattern: str) -> str:
    raw_pattern = arguments.get("pattern")
    if raw_pattern is not None and not isinstance(raw_pattern, str):
        return "text_retrieve pattern must be a string"
    raw_keywords = arguments.get("keywords")
    if raw_keywords is not None and not isinstance(raw_keywords, list):
        return "text_retrieve keywords must be a list"
    if not pattern:
        return "text_retrieve requires a non-empty pattern or keywords"
    return ""


def _safe_regex(pattern: str) -> str:
    """Use the pattern as a regex; fall back to a literal match when invalid."""
    text = str(pattern or "").strip()
    if not text:
        return r"(?!)"  # match nothing
    try:
        re.compile(text)
        return text
    except re.error:
        return re.escape(text)


def _candidate_terms(ts_code: str, company_terms: list[str] | None = None) -> list[str]:
    terms = [str(ts_code)]
    if "." in str(ts_code):
        terms.append(str(ts_code).split(".", 1)[0])
    terms.extend(str(term).strip() for term in (company_terms or []) if str(term).strip())
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            ordered.append(term)
    return ordered


def _company_terms(context: dict[str, object], ts_code: str) -> list[str]:
    terms: list[str] = [ts_code]
    for key in ("name", "name_asof", "fullname", "company_name", "short_name"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    return terms


def _bounded_int(value: object, *, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))
