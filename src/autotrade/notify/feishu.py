"""Feishu (Lark) group notifications, stdlib-only (docs/deployment_documentation.md §6).

One bot = one Feishu custom app (app_id/app_secret) that has been added to the
target group chat and granted the ``im:message:send_as_bot`` scope. Credentials
live in the gitignored ``.env``; nothing here is ever logged or committed.

Send failures must never break the caller (a worker holding at a gate, the QMT
monitor loop): every public method degrades to returning False after logging a
one-line warning to stderr.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_BASE = "https://open.feishu.cn/open-apis"


def load_dotenv_values(path: str | Path = ".env") -> dict[str, str]:
    """Minimal KEY=VALUE .env reader; strips one pair of surrounding quotes.

    The single line-parsing contract for every .env consumer in this repo
    (``pipelines.assembly.load_dotenv_into_environ`` reuses it), so a value
    written as ``KEY="value"`` reads identically everywhere."""
    values: dict[str, str] = {}
    path = Path(path)
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key.strip()] = value
    return values


class FeishuBot:
    """Tenant-token client for one bot app, pinned to one group chat."""

    def __init__(self, app_id: str, app_secret: str, chat_id: str, *, timeout_seconds: float = 10.0) -> None:
        if not (app_id and app_secret and chat_id):
            raise ValueError("FeishuBot requires app_id, app_secret and chat_id")
        self.app_id = app_id
        self._app_secret = app_secret
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds
        self._token = ""
        self._token_expires_at = 0.0

    @classmethod
    def from_env(cls, env: dict[str, str], prefix: str = "FEISHU") -> "FeishuBot | None":
        """None when the credential triple is absent (notifications disabled)."""
        app_id = env.get(f"{prefix}_APP_ID", "")
        secret = env.get(f"{prefix}_APP_SECRET", "")
        chat_id = env.get(f"{prefix}_CHAT_ID", "")
        if not (app_id and secret and chat_id):
            return None
        return cls(app_id, secret, chat_id)

    def send_text(self, text: str) -> bool:
        """Best-effort text message to the pinned group; True on delivery."""
        return self._send("text", {"text": text})

    def send_card(
        self,
        title: str,
        body: str,
        *,
        color: str = "blue",
        button_text: str | None = None,
        button_url: str | None = None,
    ) -> bool:
        """Interactive card: colored header + lark_md body + optional URL button.

        ``color`` is a Feishu header template (blue/orange/red/green/grey/
        turquoise...). Display-only cards need no callback infrastructure;
        action buttons that write back would require a public callback
        endpoint and are deliberately out of scope."""
        elements: list[dict[str, object]] = [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]
        if button_text and button_url:
            elements.append({"tag": "action", "actions": [{
                "tag": "button", "type": "primary",
                "text": {"tag": "plain_text", "content": button_text}, "url": button_url,
            }]})
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
            "elements": elements,
        }
        return self._send("interactive", card)

    def _send(self, msg_type: str, content: dict[str, object]) -> bool:
        try:
            payload = {
                "receive_id": self.chat_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            }
            result = self._call(
                f"{_BASE}/im/v1/messages?receive_id_type=chat_id", payload, token=self._tenant_token()
            )
            if result.get("code") == 0:
                return True
            print(f"feishu send failed: code={result.get('code')} msg={result.get('msg')}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - notifications must never break the caller
            print(f"feishu send failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False

    def _tenant_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        result = self._call(
            f"{_BASE}/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self._app_secret},
        )
        if result.get("code") != 0:
            raise RuntimeError(f"feishu tenant token failed: code={result.get('code')} msg={result.get('msg')}")
        self._token = str(result["tenant_access_token"])
        # Refresh two minutes early; Feishu tokens live ~2h.
        self._token_expires_at = time.time() + max(60.0, float(result.get("expire", 3600)) - 120.0)
        return self._token

    def _call(self, url: str, payload: dict[str, object], token: str | None = None) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read())


def decision_alert_card(experiment_id: str, state: str, snapshot: dict[str, object]) -> dict[str, str] | None:
    """Group-alert card for HITL worker states that need the researcher
    (docs §5.3): colored headline + experiment/session/progress context so a
    message is actionable without opening the console first. Presentation only;
    the interactive worker wires it to status-state transitions. Returns None
    for states that need no researcher attention."""
    session = str(snapshot.get("session_key") or "")
    completed = snapshot.get("completed_sessions")
    total = snapshot.get("total_sessions")
    context = f"**实验** {experiment_id}"
    if session:
        context += f"\n**会话** {session}"
    if completed is not None and total:
        context += f"\n**进度** {completed}/{total}"
    if state == "waiting_user":
        return {"title": "⏸ 会话等待批准", "color": "orange",
                "body": f"{context}\n请在控制台放行（可附研究指令）。"}
    if state == "waiting_step_user":
        summary = snapshot.get("step_summary") if isinstance(snapshot.get("step_summary"), dict) else {}
        ret = summary.get("total_return")
        metric = f"\n**验证收益** {float(ret) * 100:.2f}%" if isinstance(ret, (int, float)) else ""
        return {"title": f"🛑 Step {snapshot.get('awaiting_step')} 待批准", "color": "orange",
                "body": f"{context}{metric}\n请在控制台批准，可注入 Step 指令。"}
    if state == "waiting_user_reply":
        question = snapshot.get("awaiting_question") if isinstance(snapshot.get("awaiting_question"), dict) else {}
        body = str(question.get("question") or "")
        if len(body) > 300:
            body = body[:300] + "……"
        return {"title": f"❓ Agent 提问 #{question.get('index')}", "color": "blue",
                "body": f"{context}\n{body}\n请在控制台答复（留空=由 Agent 自行决策）。"}
    if state == "failed":
        return {"title": "❌ 实验失败", "color": "red",
                "body": f"{context}\n{snapshot.get('error')}"}
    return None
