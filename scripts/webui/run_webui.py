#!/usr/bin/env python3
"""HITL experiment console server (docs/pipeline_design.md).

Serves the web console (homepage + experiment detail SPA) and the JSON control
API over the interactive experiment pipeline. Run on the workstation that
hosts the pipeline, data, and Docker. No auth layer: production binds a Unix
domain socket in a 0700 directory (kernel-enforced single-user access on the
shared host); loopback TCP is for explicit local debugging only and is
reachable by every local user.

  ~/miniconda3/envs/quant/bin/python scripts/webui/run_webui.py --uds .runtime/webui/console.sock
"""
from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _bootstrap import add_repo_src

add_repo_src(__file__)

from autotrade.webui.server import run


def validate_tcp_bind(host: str, *, allow_unauthenticated_network: bool) -> None:
    if allow_unauthenticated_network or host.lower() == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            "the unauthenticated WebUI accepts only a loopback IP/localhost by default"
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            "refusing unauthenticated non-loopback WebUI bind; use a protected Unix "
            "socket or explicitly pass --allow-unauthenticated-network"
        )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; keep loopback unless proxied.")
    parser.add_argument("--port", type=int, default=38888, help="Listen port (default 38888).")
    parser.add_argument(
        "--allow-unauthenticated-network",
        action="store_true",
        help="Explicitly allow a non-loopback TCP bind despite the absence of authentication.",
    )
    parser.add_argument(
        "--uds",
        type=Path,
        default=None,
        help="Bind a Unix domain socket instead of TCP; access control is the socket "
        "directory's permissions (production mode; overrides --host/--port).",
    )
    parser.add_argument(
        "--experiments-root",
        type=Path,
        default=repo_root / "experiments",
        help="Experiments root directory shared with the pipeline CLIs.",
    )
    args = parser.parse_args()
    if args.uds is None:
        try:
            validate_tcp_bind(
                args.host,
                allow_unauthenticated_network=args.allow_unauthenticated_network,
            )
        except ValueError as exc:
            parser.error(str(exc))
    run(
        repo_root,
        host=args.host,
        port=args.port,
        uds=args.uds,
        experiments_root=args.experiments_root.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
