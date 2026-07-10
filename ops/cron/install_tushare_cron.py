#!/usr/bin/env python3
"""Install or refresh the managed MacroQuant TuShare cron block."""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path


BEGIN = "# BEGIN MacroQuant TuShare update"
END = "# END MacroQuant TuShare update"
REPO_ROOT = Path("/Data/lzp/MacroQuant")
TEMPLATE = REPO_ROOT / "ops/cron/tushare_update.cron"
BACKUP_DIR = REPO_ROOT / "logs" / "cron_backups"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append or refresh the MacroQuant TuShare cron block without replacing other crontab entries.")
    parser.add_argument("--dry-run", action="store_true", help="Print the resulting crontab without installing it.")
    return parser.parse_args()


def current_crontab() -> str:
    """The user's crontab; fail fast on anything but a clean read or a genuine
    'no crontab for user'. A permission/IO error treated as an empty table
    would silently wipe every unrelated job on install."""
    process = subprocess.run(["crontab", "-l"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if process.returncode == 0:
        return process.stdout
    if "no crontab for" in process.stderr.lower():
        return ""
    raise RuntimeError(f"crontab -l failed (rc={process.returncode}): {process.stderr.strip()}")


def build_managed_block() -> str:
    body = TEMPLATE.read_text(encoding="utf-8").strip()
    return f"{BEGIN}\n{body}\n{END}\n"


def replace_managed_block(current: str, managed: str) -> str:
    kept: list[str] = []
    skipping = False
    for line in current.splitlines():
        stripped = line.strip()
        if stripped == BEGIN:
            skipping = True
            continue
        if stripped == END:
            skipping = False
            continue
        if not skipping:
            kept.append(line)
    result = "\n".join(kept).rstrip()
    if result:
        result += "\n\n"
    return result + managed


def main() -> int:
    args = parse_args()
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"cron template not found: {TEMPLATE}")
    current = current_crontab()
    updated = replace_managed_block(current, build_managed_block())
    if args.dry_run:
        print(updated, end="")
        return 0
    if current.strip():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"crontab-{time.strftime('%Y%m%d-%H%M%S')}.bak"
        backup.write_text(current, encoding="utf-8")
        print(f"backed up current crontab to {backup}")
    subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
    installed = subprocess.run(["crontab", "-l"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    if BEGIN not in installed.stdout:
        raise RuntimeError("post-install verification failed: managed block not found in crontab")
    print("installed MacroQuant TuShare cron block")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
