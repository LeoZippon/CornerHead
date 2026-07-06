#!/usr/bin/env python3
"""HITL experiment console server (docs/pipeline_design.md).

Serves the web console (homepage + experiment detail SPA) and the JSON control
API over the interactive experiment pipeline. Run on the workstation that
hosts the pipeline, data, and Docker; binds 127.0.0.1 by default (no auth
layer — put a trusted reverse proxy in front for any non-local bind).

  ~/miniconda3/envs/quant/bin/python scripts/webui/run_webui.py --port 38888
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _bootstrap import add_repo_src

add_repo_src(__file__)

from autotrade.webui.server import run


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; keep loopback unless proxied.")
    parser.add_argument("--port", type=int, default=38888, help="Listen port (default 38888).")
    parser.add_argument(
        "--experiments-root",
        type=Path,
        default=repo_root / "experiments",
        help="Experiments root directory shared with the pipeline CLIs.",
    )
    args = parser.parse_args()
    run(repo_root, host=args.host, port=args.port, experiments_root=args.experiments_root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
