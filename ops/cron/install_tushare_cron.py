#!/usr/bin/env python3
"""Install or refresh the managed MacroQuant TuShare cron block."""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path


BEGIN = "# BEGIN MacroQuant TuShare update"
END = "# END MacroQuant TuShare update"
REPO_ROOT = Path("/Data/lzp/MacroQuant")
TEMPLATE = REPO_ROOT / "ops/cron/tushare_update.cron"
BACKUP_DIR = REPO_ROOT / "archive" / "crontab"


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


def validate_managed_markers(text: str, *, source: str) -> None:
    """Reject unmatched, duplicated, reversed, or nested managed markers."""
    markers = [
        line.strip()
        for line in text.splitlines()
        if line.strip() in {BEGIN, END}
    ]
    if not markers:
        return
    if markers != [BEGIN, END]:
        raise RuntimeError(
            f"invalid managed cron markers in {source}: expected one paired "
            f"{BEGIN!r}/{END!r}, found {markers!r}"
        )


def replace_managed_block(current: str, managed: str) -> str:
    validate_managed_markers(current, source="current crontab")
    validate_managed_markers(managed, source="generated block")
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


def verify_installed_crontab(expected: str, installed: str) -> None:
    validate_managed_markers(installed, source="installed crontab")
    expected_text = expected.rstrip("\n") + "\n"
    installed_text = installed.rstrip("\n") + "\n"
    if installed_text != expected_text:
        raise RuntimeError("post-install verification failed: installed crontab differs from requested content")


def write_private_backup(path: Path, content: str) -> None:
    """Store the full user crontab without exposing unrelated job secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


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
        # Source prefix + pid: two installers (or two runs) in the same second
        # can never silently overwrite each other's backup.
        backup = BACKUP_DIR / f"crontab-tushare-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.bak"
        write_private_backup(backup, current)
        print(f"backed up current crontab to {backup}")
    subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
    installed = subprocess.run(["crontab", "-l"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    verify_installed_crontab(updated, installed.stdout)
    print("installed MacroQuant TuShare cron block")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
