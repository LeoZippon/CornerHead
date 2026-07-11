#!/usr/bin/env python3
"""Live minute-bar acquisition via TuShare rt_min (thin CLI wrapper).

--probe validates interface access with one code (trial tier answers the
latest bar even off-session). --follow polls a watchlist once per interval
during trading hours and persists bars into data/raw/rt_min_live/ in the
historical minute-store schema, ready for the live tick loop.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _bootstrap import add_repo_src

REPO_ROOT = add_repo_src(__file__)

from autotrade.data_sources.tushare.common import TuShareClient, load_token
from autotrade.data_sources.tushare.realtime import (
    LIVE_MINUTE_DIRNAME,
    RealtimeMinuteFeed,
    RealtimeMinuteStore,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", default="000001.SZ", help="comma-separated watchlist")
    parser.add_argument("--probe", action="store_true", help="single poll, print bars, no persistence")
    parser.add_argument("--follow", action="store_true", help="poll + persist until --until")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--until", default="15:05", help="stop wall-clock (Asia/Shanghai HH:MM)")
    parser.add_argument("--store-dir", default=str(REPO_ROOT / "data" / "raw" / LIVE_MINUTE_DIRNAME))
    parser.add_argument("--min-interval-seconds", type=float, default=0.22)
    args = parser.parse_args()

    client = TuShareClient(load_token(REPO_ROOT), min_interval=args.min_interval_seconds, timeout=30)
    feed = RealtimeMinuteFeed(client, [code.strip() for code in args.codes.split(",") if code.strip()])
    if args.probe or not args.follow:
        bars = feed.poll()
        print(bars.to_string(index=False) if not bars.empty else "(no bars returned)")
        return 0
    store = RealtimeMinuteStore(args.store_dir)
    stop_at = dt.datetime.strptime(args.until, "%H:%M").time()
    while dt.datetime.now().time() < stop_at:
        appended = store.append(feed.poll())
        if appended:
            print(f"{dt.datetime.now():%H:%M:%S} appended {appended}", flush=True)
        time.sleep(max(1.0, args.interval_seconds))
    print("follow window ended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
