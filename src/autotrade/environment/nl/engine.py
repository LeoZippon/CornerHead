"""Host-side NL Sub Agent with point-in-time text retrieval.

``at_tools.nl(...)`` starts one bounded host-side Sub Agent task. The Sub Agent
may call the ``text_retrieve`` tool, which is backed by the snapshot
``text_index.parquet`` and ``text_library/``. The final answer is intentionally
free-form: strategy code receives the Sub Agent result and decides how to parse
or use it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from autotrade.environment.llm.proxy import LLMProxy, LLMProxyError, ProviderResponse, assistant_tool_turn
from autotrade.environment.nl.retrieval import TextRetriever
from autotrade.environment.runtime import new_id, sanitize_for_log, utc_now_iso
from autotrade.environment.tools.base import ActionField, ActionSpec, ToolSchemaError

MAX_TOOL_ROUNDS = 3
TEXT_RETRIEVE_TOOL = "text_retrieve"

TEXT_RETRIEVE_SPEC = ActionSpec(
    action=TEXT_RETRIEVE_TOOL,
    tool_name="nl_text_retrieve_tool",
    description=(
        "Retrieve point-in-time text evidence by case-insensitive grep/regex over titles, "
        "codes, and optional full text bodies. RE2 semantics: backreferences and "
        "lookaround are unsupported; patterns are capped at 256 chars."
    ),
    fields=(
        ActionField("pattern", "string", required=True, description="Case-insensitive grep/regex pattern (RE2 semantics)."),
        ActionField(
            "ts_code",
            "string",
            default="",
            description=(
                "Optional stock code that bounds retrieval to code/name-linked candidate evidence; "
                "leave empty for event, sector, macro, or market-wide searches."
            ),
        ),
        ActionField("max_results", "integer", default=5, min_value=1, max_value=20),
        ActionField("search_bodies", "boolean", default=True),
    ),
    read_only=True,
    destructive=False,
    concurrency_safe=False,
    result_policy="bounded_structured_evidence",
    allowed_modes=("nl_subagent",),
)
TEXT_RETRIEVE_SCHEMA = TEXT_RETRIEVE_SPEC.to_tool_schema()

SUB_AGENT_SYSTEM_PROMPT = """\
# Role
You are an A-share point-in-time natural-language research Sub Agent. You help
strategy code answer the user's prompt for one stock, event, sector, macro, or
decision context.

# Data Boundary
Use only the context and text evidence returned by tools in this task. Do not
use future events, price moves after the decision time, private credentials, or
unstated facts from memory. Prefer the most recent point-in-time evidence, and
remember publish/ingest time and retrieval recall are imperfect. If the evidence
is thin or absent, say so explicitly and lower your confidence instead of filling
gaps with model priors; treat free text as evidence to weigh, not an established
fact.

# Available Tool
Call the ``text_retrieve`` function tool (native function calling) to fetch text
evidence. ``pattern`` uses case-insensitive grep/regex semantics (RE2 engine:
backreferences and lookaround are unsupported; max 256 chars — an out-of-contract
pattern returns a fixable tool error) over titles, codes, and optional full text
bodies. A single-stock request is already bounded to code/name-linked evidence,
so search its event/risk concepts directly; use broad event/sector/macro patterns
for general requests. Optional arguments:
``ts_code``, ``max_results`` (1-20), ``search_bodies``. ``ts_code`` bounds a
single-stock search to code/name-linked evidence; omit it for broad context.

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
    # Absolute monotonic deadline shared with the calling decision: every
    # provider round's timeout is clamped to the remaining time and retries are
    # disabled once clamped, so an in-flight NL task cannot stretch a decision
    # far past its wall cap (worst overrun = one bounded HTTP call).
    deadline_at: float | None = None

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
            "scope": "stock" if self.ts_code else "general",
            "status": "ok" if self.ok else "error",
            "state": self.state,
            "content": self.content,
            "error": self.error,
            "rounds": self.rounds,
            "tool_calls": list(self.tool_calls),
            "evidence": list(self.evidence),
            "company_context": dict(self.company_context),
        }


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
        argument_error = _text_retrieve_argument_error(arguments, pattern)
        if argument_error:
            validated = {}
        else:
            normalized = dict(arguments)
            normalized["pattern"] = pattern
            normalized.pop("keywords", None)  # legacy compatibility; not advertised in the schema.
            try:
                validated = TEXT_RETRIEVE_SPEC.validate(normalized, mode="nl_subagent")
            except ToolSchemaError as exc:
                validated = {}
                argument_error = str(exc)
        # A stock task is a hard evidence boundary: a model-supplied code must
        # never widen or replace the strategy's requested candidate. General
        # tasks have no default and may still opt into a stock scope.
        ts_code = str(default_ts_code or validated.get("ts_code") or "").strip()
        max_results = int(validated.get("max_results", 5) or 5)
        search_bodies = bool(validated.get("search_bodies", True))
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
        search_started = time.monotonic()
        try:
            evidence = self.retriever.search(
                pattern,
                ts_code=ts_code,
                max_results=max_results,
                search_bodies=search_bodies,
                company_terms=company_terms,
            )
        except ValueError as exc:
            # Pattern outside the RE2/grep contract: fixable tool error the
            # sub-agent can retry with a simpler pattern.
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
                    "error": str(exc),
                    "hits": 0,
                    "result_ids": [],
                    "duration_seconds": round(time.monotonic() - search_started, 6),
                },
                [],
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
            "duration_seconds": round(time.monotonic() - search_started, 6),
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
        ts_code = str(ts_code or "").strip()
        task = NLSubAgentResult(ts_code=ts_code, task_id=new_id("nlsub"), state="failed")
        if ts_code:
            task.company_context = self.company_contexts.get(ts_code, {"ts_code": ts_code, "context": "unknown"})
        else:
            task.company_context = {"scope": "general", "context": "no_single_stock"}
        messages = self._initial_messages(task, prompt=prompt, request_kwargs=request_kwargs or {})
        candidate_terms = company_terms(task.company_context, ts_code)
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
                messages.append(assistant_tool_turn(response))
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
                            arguments, default_ts_code=ts_code, company_terms=candidate_terms
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
        timeout = config.per_call_timeout_seconds
        deadline_clamped = False
        if config.deadline_at is not None:
            remaining = config.deadline_at - time.monotonic()
            if remaining <= 1.0:
                raise LLMProxyError("NL task reached the decision wall-clock deadline", timeout=True)
            if remaining < timeout:
                timeout = remaining
                deadline_clamped = True
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
                timeout_seconds=timeout,
                max_tokens=config.max_tokens,
                # Near the deadline a retry cannot fit: one bounded attempt only.
                max_retries=0 if deadline_clamped else None,
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


def company_terms(context: dict[str, object], ts_code: str) -> list[str]:
    code = str(ts_code or "").strip()
    terms: list[str] = [code] if code else []
    for key in ("name", "fullname", "company_name", "short_name"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    return terms
