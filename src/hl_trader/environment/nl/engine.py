"""Natural-language scoring engine: backtest_tool's internal NL step.

Implements docs/environment_design.md 4.4 "自然语言评分内部流程":

- one isolated task per candidate stock, run in a bounded thread pool;
- at most three retrieval rounds against text_index/text_library;
- the candidate object passed to the LLM contains only ``ts_code``; factor
  scores, ranks, weights, and other stocks' conclusions never enter the prompt;
- tasks finish only in a documented terminal state; the batch completes when
  every task is terminal.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from hl_trader.environment.llm.proxy import LLMProxy, LLMProxyError
from hl_trader.environment.runtime import new_id, sanitize_for_log, utc_now_iso

from .extraction import ExtractionError, extract_json_object, validate_score_payload

TERMINAL_STATES = ("completed", "skipped_by_config", "failed_with_policy", "timeout", "failed")
MAX_RETRIEVAL_ROUNDS = 3

ROUND_INSTRUCTION = """\
# 角色
你是 A 股个股文本证据评分员。只依据决策时点已可见的文本证据，对一只候选股票给出自然语言分。\
不得使用你训练记忆中的公司后续发展、股价走势或任何未提供的信息。

# 每轮输出（只输出一个 JSON 对象，二选一）
1. 证据不足时，发起 grep 检索（大小写不敏感的正则，在标题、代码和正文全文上匹配）：
   {"search_requests": [{"pattern": "平安银行|000001.SZ|问询函|处罚|立案", "max_results": 5}]}
   pattern 支持正则替换。评估个股风险时，优先把 company_context 中的公司名、证券代码、行业/主营业务词与事件词组合；
   只有公司相关证据不足时，才用泛化行业/宏观 pattern 补充背景。背景 evidence 不能作为个股评分引用。最多 3 轮检索，每轮可发多个 pattern。
2. 证据足够时，直接给出最终评分：
   {"ts_code": "<候选代码>", "nl_score": <-1~1>, "confidence": <0~1>, "risk_tags": [...], "applied_prior_ids": [...], "evidence_ids": [...]}

# 评分含义
- nl_score：0 为中性；正数支持做多，负数支持降权、回避或做空；幅度反映证据强度。
- confidence：证据充分性与一致性；公司信息不足时必须降低 confidence 并扩大检索，而不是凭常识猜测。
- risk_tags：如 regulatory_risk / litigation / earnings_miss / pledge_risk；证据极端负面且不可持有时加 "hard_exclude"。
- applied_prior_ids：本次实际用到的投资先验规则 id，必须来自 prior_rules；非中性或引用证据的评分至少引用一条适用规则。
- evidence_ids：只能引用本会话中检索返回且标记为 candidate 的 text_id 或 source_hash；background evidence 只能辅助理解行业背景，不能作为个股评分引用。没有候选公司证据就留空并降低 confidence，严禁编造引用。\
"""
FINAL_INSTRUCTION = """\
现在直接给出最终评分。只输出一个严格 JSON 对象，字段为 \
{"ts_code", "nl_score", "confidence", "risk_tags", "applied_prior_ids", "evidence_ids"}。\
applied_prior_ids 必须来自 prior_rules；非中性或引用证据的评分至少引用一条 prior；evidence_ids 只能来自本任务检索返回且标记为 candidate 的 evidence。不要任何其他文字。\
"""
REPAIR_INSTRUCTION = "上一条回复不是合法的单个 JSON 对象。请只输出一个严格 JSON 对象，包含全部必需字段，不要任何其他文字。"


@dataclass(frozen=True)
class NLScoringConfig:
    mode: str = "on"  # off | sample | on
    sample_size: int = 3
    per_call_timeout_seconds: float = 300.0
    max_workers: int = 4
    # Reasoning tokens count against this; keep headroom beyond the JSON itself.
    max_tokens: int = 3000
    allow_repair_call: bool = True
    # "fail" fails the formal backtest on any failed task; "neutral_with_audit"
    # is the explicit, auditable failure-handling policy from the run config.
    failure_policy: str = "fail"

    def __post_init__(self) -> None:
        if self.mode not in {"off", "sample", "on"}:
            raise ValueError(f"unsupported nl mode={self.mode}")
        if self.failure_policy not in {"fail", "neutral_with_audit"}:
            raise ValueError(f"unsupported failure_policy={self.failure_policy}")


@dataclass
class NLTaskResult:
    ts_code: str
    task_id: str
    state: str
    score: dict[str, object] | None = None
    error: str = ""
    rounds: int = 0
    early_stop_reason: str = ""
    search_requests: list[dict[str, object]] = field(default_factory=list)
    evidence: list[dict[str, object]] = field(default_factory=list)
    llm_calls: list[dict[str, object]] = field(default_factory=list)
    company_context: dict[str, object] = field(default_factory=dict)


@dataclass
class NLBatchResult:
    results: list[NLTaskResult]
    mode: str

    @property
    def summary(self) -> dict[str, object]:
        states: dict[str, int] = {}
        for result in self.results:
            states[result.state] = states.get(result.state, 0) + 1
        return {
            "mode": self.mode,
            "candidates": len(self.results),
            "states": states,
            "completed": states.get("completed", 0),
            "failed": states.get("failed", 0),
            "timeout": states.get("timeout", 0),
        }

    def scores_by_code(self) -> dict[str, dict[str, object]]:
        return {r.ts_code: r.score for r in self.results if r.score is not None}

    def has_blocking_failure(self) -> bool:
        return any(r.state == "failed" for r in self.results)


class TextRetriever:
    """Grep-style retrieval over the snapshot text index and as-of text library.

    The scoring LLM supplies a regex pattern (case-insensitive grep semantics).
    Titles/codes are matched first, then full bodies when more results are
    needed; results rank title hits before body-only hits, recency second.
    Bodies live in per-dataset parquet shards under text_library/, lazily
    loaded and cached; the default snippet returns the full stored body
    (capped at 4000 characters at snapshot build time).
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
        # Candidate-related title/code hits outrank broad market hits. Broad
        # hits remain available as a fallback, but never displace own evidence.
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
                        str(row.get("dataset", "")), str(row.get("text_id", "")), ts_code=ts_code, company_terms=company_terms
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


def _request_pattern(request: object) -> str:
    """Pattern from a search request; legacy keyword lists become alternations."""
    if not isinstance(request, dict):
        return ""
    pattern = str(request.get("pattern", "") or "").strip()
    if pattern:
        return pattern
    keywords = [str(k).strip() for k in request.get("keywords", []) if str(k).strip()]
    return "|".join(re.escape(keyword) for keyword in keywords)


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


class NLScoringEngine:
    def __init__(
        self,
        proxy: LLMProxy,
        retriever: TextRetriever,
        *,
        prior_rules: list[dict[str, object]],
        scoring_readme: str,
        company_contexts: dict[str, dict[str, object]],
    ) -> None:
        self.proxy = proxy
        self.retriever = retriever
        self.prior_rules = prior_rules
        self.scoring_readme = scoring_readme
        self.company_contexts = company_contexts

    def score_candidates(self, ts_codes: list[str], config: NLScoringConfig) -> NLBatchResult:
        if config.mode == "off":
            results = [
                NLTaskResult(ts_code=code, task_id=new_id("nltask"), state="skipped_by_config", error="nl_mode=off")
                for code in ts_codes
            ]
            return NLBatchResult(results=results, mode=config.mode)
        selected = ts_codes if config.mode == "on" else ts_codes[: config.sample_size]
        skipped = [code for code in ts_codes if code not in selected]
        with ThreadPoolExecutor(max_workers=max(1, config.max_workers)) as pool:
            futures = {code: pool.submit(self._run_task, code, config) for code in selected}
            results = [futures[code].result() for code in selected]
        results.extend(
            NLTaskResult(ts_code=code, task_id=new_id("nltask"), state="skipped_by_config", error="nl_mode=sample")
            for code in skipped
        )
        return NLBatchResult(results=results, mode=config.mode)

    # ---- single-stock task ----

    def _run_task(self, ts_code: str, config: NLScoringConfig) -> NLTaskResult:
        task = NLTaskResult(ts_code=ts_code, task_id=new_id("nltask"), state="failed")
        task.company_context = self.company_contexts.get(ts_code, {"ts_code": ts_code, "context": "unknown"})
        seen_ids: set[str] = set()
        evidence_seen: set[str] = set()
        try:
            payload = None
            for round_index in range(1, MAX_RETRIEVAL_ROUNDS + 1):
                task.rounds = round_index
                payload = self._call(task, self._round_messages(task), config, purpose=f"retrieval_round_{round_index}")
                if "search_requests" not in payload:
                    break  # early final JSON ends the task; no further retrieval rounds
                requests = payload["search_requests"]
                if not isinstance(requests, list):
                    raise ExtractionError("search_requests must be a list")
                for request in requests:
                    pattern = _request_pattern(request)
                    max_results = int(request.get("max_results", 5)) if isinstance(request, dict) else 5
                    found = self.retriever.search(
                        pattern,
                        ts_code=ts_code,
                        max_results=max_results,
                        company_terms=_company_terms(task.company_context, ts_code),
                    )
                    task.search_requests.append(
                        {"round": round_index, "pattern": pattern, "max_results": max_results, "hits": len(found)}
                    )
                    for item in found:
                        if item["text_id"] not in evidence_seen:
                            evidence_seen.add(item["text_id"])
                            # Scores may cite text_id or source_hash (docs/environment_design.md 4.4).
                            if item.get("relevance") == "candidate":
                                seen_ids.add(str(item["text_id"]))
                            if item.get("relevance") == "candidate" and item.get("source_hash"):
                                seen_ids.add(str(item["source_hash"]))
                            task.evidence.append(item)
                payload = None
            if payload is None:
                payload = self._call(task, self._final_messages(task), config, purpose="final_score")
            task.score = self._validate_with_repair(task, payload, seen_ids, config)
            task.state = "completed"
            if task.rounds < MAX_RETRIEVAL_ROUNDS:
                task.early_stop_reason = "sufficient_evidence_or_early_final_json"
        except LLMProxyError as exc:
            task.state = "timeout" if exc.timeout else self._failure_state(config)
            task.error = str(exc)
        except ExtractionError as exc:
            task.state = self._failure_state(config)
            task.error = str(exc)
            if config.failure_policy == "neutral_with_audit":
                task.score = _neutral_score(ts_code)
        return task

    def _validate_with_repair(
        self,
        task: NLTaskResult,
        payload: dict[str, object],
        seen_ids: set[str],
        config: NLScoringConfig,
    ) -> dict[str, object]:
        try:
            return validate_score_payload(
                payload,
                expected_ts_code=task.ts_code,
                seen_evidence_ids=seen_ids,
                valid_prior_ids=self._prior_ids(),
                require_prior_id=bool(self._prior_ids()),
            )
        except ExtractionError as exc:
            if not config.allow_repair_call:
                raise
            repair_messages = self._final_messages(task) + [{"role": "user", "content": f"{REPAIR_INSTRUCTION} Error: {exc}"}]
            repaired = self._call(task, repair_messages, config, purpose="repair_call")
            return validate_score_payload(
                repaired,
                expected_ts_code=task.ts_code,
                seen_evidence_ids=seen_ids,
                valid_prior_ids=self._prior_ids(),
                require_prior_id=bool(self._prior_ids()),
            )

    def _call(
        self,
        task: NLTaskResult,
        messages: list[dict[str, str]],
        config: NLScoringConfig,
        *,
        purpose: str,
    ) -> dict[str, object]:
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
            response = self.proxy.complete(
                messages,
                json_mode=True,
                timeout_seconds=config.per_call_timeout_seconds,
                max_tokens=config.max_tokens,
            )
        except Exception as exc:
            detail.update(status="error", error=str(exc), completed_at=utc_now_iso())
            task.llm_calls.append(detail)
            raise
        extracted = extract_json_object(response.content)
        detail.update(
            status="ok",
            completed_at=utc_now_iso(),
            raw_content=response.content,
            reasoning_content=response.reasoning_content or extracted.stripped_think,
            parsed=sanitize_for_log(extracted.payload),
            usage=response.usage,
        )
        task.llm_calls.append(detail)
        return extracted.payload

    def _round_messages(self, task: NLTaskResult) -> list[dict[str, str]]:
        body = {
            "candidate": {"ts_code": task.ts_code},
            "company_context": task.company_context,
            "prior_rules": self.prior_rules,
            "evidence": task.evidence,
        }
        return [
            {"role": "system", "content": f"{ROUND_INSTRUCTION}\n\nScoring notes:\n{self.scoring_readme}"},
            {"role": "user", "content": json.dumps(body, ensure_ascii=False, sort_keys=True)},
        ]

    def _final_messages(self, task: NLTaskResult) -> list[dict[str, str]]:
        return self._round_messages(task) + [{"role": "user", "content": FINAL_INSTRUCTION}]

    @staticmethod
    def _failure_state(config: NLScoringConfig) -> str:
        return "failed_with_policy" if config.failure_policy == "neutral_with_audit" else "failed"

    def _prior_ids(self) -> set[str]:
        return {str(rule.get("id", "")).strip() for rule in self.prior_rules if str(rule.get("id", "")).strip()}


def _neutral_score(ts_code: str) -> dict[str, object]:
    return {
        "ts_code": ts_code,
        "nl_score": 0.0,
        "confidence": 0.0,
        "risk_tags": ["nl_failure_policy_neutral"],
        "applied_prior_ids": [],
        "evidence_ids": [],
    }
