#!/usr/bin/env python3
"""QMT live sync + Feishu fill notifications (docs/deployment_documentation.md §6).

Loops: scp-pull the QMT node's outbox into data/qmt_live/, notify the group per
new fill via the dedicated FEISHU_QMT_* bot. Run under ops/qmt/qmt_monitor.sh.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _bootstrap import add_repo_src

add_repo_src(__file__)

from autotrade.live import QmtLiveMonitor
from autotrade.notify import FeishuBot, load_dotenv_values


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync QMT live state and notify fills.")
    parser.add_argument("--interval-seconds", type=float, default=20.0)
    parser.add_argument("--local-dir", type=Path, default=Path("data/qmt_live"))
    parser.add_argument("--ssh-dest", default="")
    parser.add_argument("--remote-outbox", default=os.environ.get("QMT_REMOTE_OUTBOX", "C:/xquant/outbox"))
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit (smoke test)")
    args = parser.parse_args()

    env = {**load_dotenv_values(), **os.environ}
    bot = FeishuBot.from_env(env, prefix="FEISHU_QMT")
    if bot is None:
        print("FEISHU_QMT_APP_ID/APP_SECRET/CHAT_ID missing in .env; refusing to run blind", file=sys.stderr)
        return 1
    # The node's ssh identity is deployment config, never a repo constant
    # (deployment docs: the repo stores no QMT login identities).
    ssh_dest = args.ssh_dest or env.get("QMT_SSH_DEST", "")
    if not ssh_dest:
        print("QMT_SSH_DEST missing (.env or --ssh-dest), e.g. <user>@<qmt-host>", file=sys.stderr)
        return 1
    monitor = QmtLiveMonitor(
        local_dir=args.local_dir, notify=bot.send_text,
        ssh_dest=ssh_dest, remote_outbox=args.remote_outbox,
    )
    print(f"qmt_live_monitor: {ssh_dest}:{args.remote_outbox} -> {args.local_dir} every {args.interval_seconds}s")
    while True:
        result = monitor.run_once()
        if result["notified"] or result["error"]:
            print(f"{time.strftime('%F %T')} {result}")
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
