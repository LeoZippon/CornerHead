#!/usr/bin/env python3
"""Interactive (HITL) experiment worker entrypoint (docs/pipeline_design.md).

Runs one experiment's gated Epoch/Fold/Held-out loop from the parameters in
``experiments/<id>/hitl/params.json``, honouring ``control.json`` (pause /
step approvals / per-session directives / stop) and reporting position and
heartbeats to ``status.json``. Normally spawned detached by the web console
(``scripts/webui/run_webui.py``); can also be launched manually for a headless
HITL run driven purely through the control file.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
_HERE = Path(__file__).resolve().parent
for _path in (_SCRIPTS, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _bootstrap import add_repo_src

add_repo_src(__file__)

from autotrade.pipelines.interactive import run_interactive_worker


def _terminate(signum, frame):  # noqa: ANN001 - signal handler signature
    raise SystemExit(128 + signum)


def _restore_child_reaping() -> None:
    """Undo the console parent's SIGCHLD=SIG_IGN inheritance for this worker."""
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)


def main() -> int:
    _restore_child_reaping()
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        required=True,
        help="experiments/<experiment_id> directory containing hitl/params.json",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="control.json polling interval while paused or waiting for approval",
    )
    args = parser.parse_args()
    # A graceful SIGTERM unwinds through the pipeline's finally blocks (docker
    # stop) and lets the worker record state="stopped" before exiting.
    signal.signal(signal.SIGTERM, _terminate)
    result = run_interactive_worker(
        args.experiment_dir.resolve(),
        repo_root=repo_root,
        poll_seconds=args.poll_seconds,
    )
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
