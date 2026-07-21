"""Strict JSON-object extraction from provider responses.

Only tool-call arguments, JSON-mode content, or a single JSON object in plain
text are accepted. One ```json fence may be removed. Closed <think> blocks are
stripped and preserved for logging; an unclosed <think> or multiple JSON objects
is a failure. Thinking text never supplies formal fields.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
FENCE_PATTERN = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)

class ExtractionError(ValueError):
    """The provider response does not contain exactly one valid JSON object."""


@dataclass(frozen=True)
class ExtractedJSON:
    payload: dict[str, object]
    stripped_think: str = ""


def extract_json_object(content: str, *, tool_call_arguments: str | None = None) -> ExtractedJSON:
    """Extract exactly one JSON object from a provider response.

    ``tool_call_arguments`` takes precedence when the provider returned a
    tool/function call; otherwise the content must itself be one JSON object,
    optionally wrapped in a single json code fence and/or preceded by a closed
    think block.
    """
    if tool_call_arguments is not None:
        return ExtractedJSON(payload=_loads_object(tool_call_arguments))
    text = content.strip()
    stripped_think = ""
    if THINK_OPEN in text:
        open_index = text.index(THINK_OPEN)
        close_index = text.find(THINK_CLOSE, open_index)
        if close_index < 0:
            raise ExtractionError("unclosed <think> block in provider response")
        stripped_think = text[open_index + len(THINK_OPEN) : close_index]
        text = (text[:open_index] + text[close_index + len(THINK_CLOSE) :]).strip()
        if THINK_OPEN in text:
            raise ExtractionError("multiple <think> blocks in provider response")
    fence = FENCE_PATTERN.match(text)
    if fence:
        text = fence.group(1).strip()
    return ExtractedJSON(payload=_loads_object(text), stripped_think=stripped_think)


def _loads_object(text: str) -> dict[str, object]:
    try:
        decoded, end = json.JSONDecoder().raw_decode(text.strip())
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"response is not valid JSON: {exc}") from exc
    if text.strip()[end:].strip():
        raise ExtractionError("response contains content beyond a single JSON object")
    if not isinstance(decoded, dict):
        raise ExtractionError("response JSON must be a single object")
    return decoded
