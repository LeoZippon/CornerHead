"""Shared HTTP response stub for tests that patch ``urlopen`` / URL openers.

A single positional argument carries the response content and is interpreted by
type, so the historical per-file call sites keep working unchanged:

- ``FakeHTTPResponse({"data": ...})`` — a mapping (or any non-bytes/str object)
  is JSON-encoded; ``read()`` returns the UTF-8 JSON bytes.
- ``FakeHTTPResponse(b"<html>...")`` / ``FakeHTTPResponse("text")`` — a bytes or
  str body is returned verbatim by ``read(size)`` (optionally size-limited).

``status`` and a ``Content-Type`` header are always exposed so consumers that
read ``.status`` or ``.headers`` work regardless of the construction style.
"""

from __future__ import annotations

import json
from email.message import Message


class FakeHTTPResponse:
    def __init__(self, content: object = b"", *, status: int = 200, content_type: str = "text/html") -> None:
        if isinstance(content, (bytes, bytearray)):
            self._body = bytes(content)
        elif isinstance(content, str):
            self._body = content.encode("utf-8")
        else:
            self._body = json.dumps(content).encode("utf-8")
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return self._body
        return self._body[:size]
