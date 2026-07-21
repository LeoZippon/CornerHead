"""Host-side managed ctx.state_dir staging (docs/environment_design.md, check.md W4).

A strategy writes cross-tick state to ctx.state_dir. Inside a ctx.substep(name, B),
that path resolves (in the sandbox driver) to a hidden staging directory, so the
write is captured rather than landing in the visible state directory immediately.
The driver reports the staged files each tick; this stager merges each into the
visible directory only once ready_at = generating-tick + B has elapsed, modelling
the latency before a heavy block's output is usable. Later-generated writes win on
conflict. Unmerged-at-region-end writes are kept in the audit ledger.

Both directories are rebuilt empty per backtest, so each replay is independent and
reproducible; durable cross-backtest data belongs in models/, not state_dir.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

_MAX_STATE_FILE_BYTES = 64 * 1024 * 1024


def _contained(root: Path, rel: str) -> Path:
    candidate = root / rel
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"staged state path escapes {root.name}: {rel!r}")
    return candidate


@dataclass
class _Staged:
    seq: int
    state_rel: str
    staging_rel: str
    substep: str
    budget_minutes: float
    gen_at: str
    ready_at: datetime
    status: str = "pending"
    merged_at: str = ""
    file_hash: str = ""


@dataclass
class StateStager:
    visible_dir: Path
    staging_dir: Path
    _pending: list[_Staged] = field(default_factory=list)
    _ledger: list[_Staged] = field(default_factory=list)
    _applied_seq: dict[str, int] = field(default_factory=dict)
    _seq: int = 0

    def __post_init__(self) -> None:
        # Fresh per backtest: never read a prior run's leftover state.
        for path in (self.visible_dir, self.staging_dir):
            path.mkdir(parents=True, exist_ok=True)
            # Preserve the root inode: formal Docker binds these exact
            # directories before the host stager initializes them.
            for child in path.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            # Docker sandboxes run strategy code as the container ``agent`` user,
            # which maps to a subuid under rootless Docker. These host-managed
            # scratch directories therefore must be world-writable, like
            # workspace/output/models, or ctx.state_dir writes fail before the
            # strategy can be evaluated.
            path.chmod(0o777)

    def register(self, staged: list[dict[str, object]], *, when: datetime) -> None:
        """Record this tick's staged writes; ready_at = ``when`` + declared budget."""
        for item in staged:
            try:
                budget = float(item.get("budget_minutes", 0.0) or 0.0)
            except (TypeError, ValueError):
                budget = 0.0
            staging_rel = str(item.get("staging_rel", ""))
            state_rel = str(item.get("state_rel", ""))
            if not staging_rel or not state_rel:
                continue
            # The rel paths come from the sandbox driver's tick report, i.e. from
            # agent-controlled code: containment is enforced HERE, host-side.
            # resolve() also follows symlinks, so a staged link pointing outside
            # either root is rejected at registration.
            _contained(self.staging_dir, staging_rel)
            _contained(self.visible_dir, state_rel)
            record = _Staged(
                seq=self._seq,
                state_rel=state_rel,
                staging_rel=staging_rel,
                substep=str(item.get("substep", "")),
                budget_minutes=budget,
                gen_at=when.isoformat(),
                ready_at=when + timedelta(minutes=budget),
                status="pending",
            )
            self._seq += 1
            self._pending.append(record)
            self._ledger.append(record)

    def merge_ready(self, when: datetime) -> int:
        """Merge every staged write whose ready_at has arrived by ``when``.

        Applies in generation order and only when this write is the newest seen for
        its target, so a later-generated write always wins even if an earlier one's
        ready_at is later. Returns the number of files merged."""
        ready = sorted((r for r in self._pending if r.ready_at <= when), key=lambda r: r.seq)
        merged = 0
        for record in ready:
            self._pending.remove(record)
            if self._applied_seq.get(record.state_rel, -1) > record.seq:
                record.status = "superseded"
                continue
            try:
                record.file_hash = _secure_merge_file(
                    self.staging_dir,
                    record.staging_rel,
                    self.visible_dir,
                    record.state_rel,
                )
            except FileNotFoundError:
                record.status = "missing_staging_file"
                continue
            except OSError as exc:
                if exc.errno not in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
                    raise
                record.status = "rejected_not_regular_file"
                continue
            except ValueError:
                # Every path component and the final source are opened relative
                # to already-open directory FDs with O_NOFOLLOW. A parent swap,
                # symlink, directory or special file therefore fails here even
                # if it happened after registration.
                record.status = "rejected_not_regular_file"
                continue
            self._applied_seq[record.state_rel] = record.seq
            record.status = "merged"
            record.merged_at = when.isoformat()
            merged += 1
        return merged

    def audit(self) -> list[dict[str, object]]:
        """Audit record for every staged write, merged or still pending at region end."""
        records: list[dict[str, object]] = []
        for record in self._ledger:
            status = "unmerged_at_region_end" if record.status == "pending" else record.status
            records.append(
                {
                    "state_rel": record.state_rel,
                    "substep": record.substep,
                    "budget_minutes": record.budget_minutes,
                    "generated_at": record.gen_at,
                    "ready_at": record.ready_at.isoformat(),
                    "merged": status == "merged",
                    "status": status,
                    "merged_at": record.merged_at,
                    "file_hash": record.file_hash,
                }
            )
        return records


def _relative_parts(rel: str) -> tuple[str, ...]:
    path = Path(rel)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid staged state relative path: {rel!r}")
    return tuple(path.parts)


def _open_parent(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o777, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _secure_merge_file(staging_root: Path, staging_rel: str, visible_root: Path, state_rel: str) -> str:
    """Copy one staged regular file through no-follow dirfds, then unlink it.

    Holding each directory/file descriptor makes later rename/symlink swaps
    irrelevant: resolution never restarts from an attacker-controlled pathname.
    The destination is atomically replaced inside its verified parent directory.
    """
    source_parts = _relative_parts(staging_rel)
    destination_parts = _relative_parts(state_rel)
    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    staging_fd = os.open(staging_root, root_flags)
    visible_fd = os.open(visible_root, root_flags)
    source_parent = destination_parent = source_fd = -1
    temp_name = f".{destination_parts[-1]}.{uuid.uuid4().hex[:12]}.tmp"
    temp_created = False
    try:
        source_parent = _open_parent(staging_fd, source_parts[:-1], create=False)
        source_fd = os.open(
            source_parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=source_parent,
        )
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("staged state source is not a regular file")
        if source_stat.st_size > _MAX_STATE_FILE_BYTES:
            raise ValueError(
                f"staged state file exceeds {_MAX_STATE_FILE_BYTES} bytes"
            )
        destination_parent = _open_parent(visible_fd, destination_parts[:-1], create=True)
        output_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o666,
            dir_fd=destination_parent,
        )
        temp_created = True
        digest = hashlib.sha256()
        with os.fdopen(os.dup(source_fd), "rb") as source, os.fdopen(output_fd, "wb") as output:
            remaining = source_stat.st_size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("staged state file changed size during merge")
                digest.update(chunk)
                output.write(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        after_stat = os.fstat(source_fd)
        if (
            after_stat.st_size != source_stat.st_size
            or after_stat.st_mtime_ns != source_stat.st_mtime_ns
            or after_stat.st_ctime_ns != source_stat.st_ctime_ns
        ):
            raise ValueError("staged state file changed during merge")
        os.replace(
            temp_name,
            destination_parts[-1],
            src_dir_fd=destination_parent,
            dst_dir_fd=destination_parent,
        )
        temp_created = False
        try:
            os.unlink(source_parts[-1], dir_fd=source_parent)
        except FileNotFoundError:
            pass
        return f"sha256:{digest.hexdigest()}"
    finally:
        if temp_created and destination_parent >= 0:
            try:
                os.unlink(temp_name, dir_fd=destination_parent)
            except FileNotFoundError:
                pass
        for fd in (source_fd, source_parent, destination_parent, staging_fd, visible_fd):
            if fd >= 0:
                os.close(fd)
