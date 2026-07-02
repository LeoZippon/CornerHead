"""Strategy and model artifact contracts, hashing, and modification diffs.

The formal strategy artifact is the ``output/`` directory. ``main.py`` at the
root is the only required entrypoint; helper modules and subpackages are
ordinary Agent-editable code, not separate artifact classes. Inherited model
parameters live in the sibling ``models/`` directory so binary state is
validated and frozen separately from strategy code.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from autotrade.environment.runtime import RUNTIME_CACHE_DIR_NAMES, RUNTIME_CACHE_SUFFIXES

REQUIRED_FILES = ("main.py",)
ARTIFACT_METADATA_FILES = frozenset({"manifest.json"})
READONLY_FILES = frozenset({"README.md"})
ALLOWED_SUFFIXES = frozenset({".py", ".json", ".md", ".txt", ".toml", ".yaml", ".yml"})
# Deny-by-default allowlist for the frozen, inheritable ``models/`` directory.
# Covers mainstream parameter/weight/serialization formats; executables, shared
# libraries, scripts, and archives are excluded by design to keep the inherited
# artifact auditable. Anti-overfit/anti-dump is enforced by byte caps + PIT
# visibility + held-out, not by suffix.
MODEL_ARTIFACT_ALLOWED_SUFFIXES = frozenset(
    {
        ".bin",
        ".cbm",
        ".ckpt",
        ".csv",
        ".gguf",
        ".h5",
        ".hdf5",
        ".joblib",
        ".json",
        ".keras",
        ".model",
        ".msgpack",
        ".npy",
        ".npz",
        ".onnx",
        ".params",
        ".pb",
        ".pdparams",
        ".pickle",
        ".pkl",
        ".pt",
        ".pth",
        ".safetensors",
        ".tflite",
        ".toml",
        ".txt",
        ".ubj",
        ".yaml",
        ".yml",
    }
)
# Mount paths a formal strategy must never hardcode. "/mnt/snapshots/" (plural,
# the staged alias root) and "/mnt/runtime/" are not mounted into the formal run
# and are kept here to fail fast if the agent copies them from prompts/docs. The
# singular "/mnt/snapshot" is intentionally absent: it is the legitimate formal
# read root (see sandbox.py formal_strategy_read_roots).
FORBIDDEN_CODE_REFERENCES = ("/mnt/snapshots/", "/mnt/runtime/", "/mnt/artifacts")
MAX_PROMPT_CHARS = 8000


class ArtifactError(ValueError):
    """A strategy artifact violates the documented format contract."""


@dataclass(frozen=True)
class StrategyArtifact:
    root: Path
    files: tuple[str, ...]
    artifact_hash: str


@dataclass(frozen=True)
class ModelArtifacts:
    root: Path
    files: tuple[str, ...]
    artifact_hash: str
    total_bytes: int


def load_strategy_artifact(root: str | Path) -> StrategyArtifact:
    """Load and validate the ``output/`` strategy artifact directory."""
    root = Path(root)
    files = _artifact_files(root)
    for name in REQUIRED_FILES:
        if name not in files:
            raise ArtifactError(f"missing required artifact file: {name}")
    main_py = root / "main.py"
    main_functions = defined_function_names(main_py)
    if "main" not in main_functions:
        raise ArtifactError("main.py must define main(ctx)")
    python_files = [root / relpath for relpath in files if relpath.endswith(".py")]
    for needle in FORBIDDEN_CODE_REFERENCES:
        if any(needle in value for path in python_files for value in _runtime_string_constants(path)):
            raise ArtifactError(f"formal strategy code must not reference stage directories: {needle}")
    prompt_path = root / "nl_prompt.md"
    if prompt_path.exists() and len(prompt_path.read_text(encoding="utf-8", errors="replace")) > MAX_PROMPT_CHARS:
        raise ArtifactError(f"nl_prompt.md must be at most {MAX_PROMPT_CHARS} characters")
    return StrategyArtifact(root=root, files=tuple(sorted(files)), artifact_hash=artifact_hash(root))


def load_model_artifacts(root: str | Path) -> ModelArtifacts:
    """Load and validate the optional ``models/`` artifact directory."""
    root = Path(root)
    files = _model_artifact_files(root, missing_ok=True)
    return ModelArtifacts(
        root=root,
        files=tuple(sorted(files)),
        artifact_hash=model_artifact_hash(root),
        total_bytes=sum((root / relpath).stat().st_size for relpath in files),
    )


def artifact_hash(root: str | Path) -> str:
    """Deterministic aggregate hash over all formal Agent output files."""
    root = Path(root)
    digest = hashlib.sha256()
    for relpath in sorted(_artifact_files(root)):
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\x00")
        digest.update((root / relpath).read_bytes())
        digest.update(b"\x00")
    return f"sha256:{digest.hexdigest()}"


def model_artifact_hash(root: str | Path) -> str:
    """Deterministic aggregate hash over optional model artifact files."""
    root = Path(root)
    digest = hashlib.sha256()
    for relpath in sorted(_model_artifact_files(root, missing_ok=True)):
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\x00")
        digest.update((root / relpath).read_bytes())
        digest.update(b"\x00")
    return f"sha256:{digest.hexdigest()}"


def combined_artifact_hash(strategy_hash: str, model_hash: str) -> str:
    """Stable identity for a strategy-code artifact plus model parameters."""
    digest = hashlib.sha256()
    digest.update(str(strategy_hash).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(model_hash).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def init_from_template(template_dir: str | Path, dest_root: str | Path) -> None:
    """Initialize ``output/`` from ``configs/agent_output_template/``."""
    template_dir = Path(template_dir)
    dest_root = Path(dest_root)
    relpaths = _artifact_files(template_dir, reject_runtime_cache=False)
    _replace_artifact_root(dest_root)
    _copy_formal_files(template_dir, dest_root, relpaths)


def copy_artifact(source_root: str | Path, dest_root: str | Path) -> None:
    """Copy one strategy artifact directory, replacing any existing copy."""
    source_root = Path(source_root)
    dest_root = Path(dest_root)
    relpaths = _artifact_files(source_root)
    _replace_artifact_root(dest_root)
    _copy_formal_files(source_root, dest_root, relpaths)


def clear_model_artifacts(dest_root: str | Path) -> None:
    """Replace the model artifact directory with an empty validated directory."""
    _replace_artifact_root(Path(dest_root))


def copy_model_artifacts(source_root: str | Path | None, dest_root: str | Path) -> None:
    """Copy optional model artifact directories, replacing any existing copy."""
    dest_root = Path(dest_root)
    if source_root is None:
        clear_model_artifacts(dest_root)
        return
    source_root = Path(source_root)
    relpaths = _model_artifact_files(source_root, missing_ok=True)
    _replace_artifact_root(dest_root)
    _copy_formal_files(source_root, dest_root, relpaths)


@dataclass(frozen=True)
class ModificationDelta:
    changed_files: tuple[str, ...]
    diff_lines: int
    code_diff_lines: int
    total_files: int
    total_bytes: int
    readonly_violations: tuple[str, ...]

    def to_record(self) -> dict[str, object]:
        return {
            "changed_files": list(self.changed_files),
            "changed_file_count": len(self.changed_files),
            "diff_lines": self.diff_lines,
            "code_diff_lines": self.code_diff_lines,
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "readonly_violations": list(self.readonly_violations),
    }


@dataclass(frozen=True)
class ModelArtifactDelta:
    changed_files: tuple[str, ...]
    total_files: int
    total_bytes: int

    def to_record(self) -> dict[str, object]:
        return {
            "changed_files": list(self.changed_files),
            "changed_file_count": len(self.changed_files),
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
        }


def modification_delta(parent_root: str | Path, work_root: str | Path) -> ModificationDelta:
    """Deterministic file and line counts for ``output`` changes."""
    parent_root = Path(parent_root)
    work_root = Path(work_root)
    parent_files = _artifact_files(parent_root) if parent_root.exists() and any(parent_root.iterdir()) else set()
    work_files = _artifact_files(work_root)
    changed: list[str] = []
    diff_lines = 0
    code_diff_lines = 0
    readonly_violations: list[str] = []
    for relpath in sorted(parent_files | work_files):
        parent_text = _read_text(parent_root / relpath) if relpath in parent_files else None
        work_text = _read_text(work_root / relpath) if relpath in work_files else None
        if parent_text == work_text:
            continue
        changed.append(relpath)
        if relpath in READONLY_FILES:
            readonly_violations.append(relpath)
        line_delta = _changed_line_count(parent_text or "", work_text or "")
        diff_lines += line_delta
        if relpath.endswith(".py"):
            code_diff_lines += line_delta
    return ModificationDelta(
        changed_files=tuple(changed),
        diff_lines=diff_lines,
        code_diff_lines=code_diff_lines,
        total_files=len(work_files),
        total_bytes=sum((work_root / relpath).stat().st_size for relpath in work_files),
        readonly_violations=tuple(readonly_violations),
    )


def model_artifact_delta(parent_root: str | Path, work_root: str | Path) -> ModelArtifactDelta:
    """Deterministic changed-file counts for optional model parameter files."""
    parent_root = Path(parent_root)
    work_root = Path(work_root)
    parent_files = _model_artifact_files(parent_root, missing_ok=True)
    work_files = _model_artifact_files(work_root, missing_ok=True)
    changed: list[str] = []
    for relpath in sorted(parent_files | work_files):
        if relpath not in parent_files or relpath not in work_files:
            changed.append(relpath)
        elif (parent_root / relpath).read_bytes() != (work_root / relpath).read_bytes():
            changed.append(relpath)
    return ModelArtifactDelta(
        changed_files=tuple(changed),
        total_files=len(work_files),
        total_bytes=sum((work_root / relpath).stat().st_size for relpath in work_files),
    )


@dataclass(frozen=True)
class ModificationConstraints:
    """Per-Step/Fold limits over ``output`` and optional model parameters."""

    max_changed_files: int = 8
    max_diff_lines: int = 600
    max_code_diff_lines: int = 500
    max_strategy_files: int = 64
    max_strategy_bytes: int = 1_000_000
    max_model_artifact_files: int = 64
    max_model_artifact_bytes: int = 1024 * 1024 * 1024
    early_epoch_count: int = 2
    early_max_changed_files: int = 12
    early_max_diff_lines: int = 1200
    early_max_code_diff_lines: int = 1000
    is_initial_artifact: bool = False

    def for_epoch(self, epoch_index: int) -> "ModificationConstraints":
        if self.is_initial_artifact or epoch_index <= self.early_epoch_count:
            return ModificationConstraints(
                max_changed_files=self.early_max_changed_files,
                max_diff_lines=self.early_max_diff_lines,
                max_code_diff_lines=self.early_max_code_diff_lines,
                max_strategy_files=self.max_strategy_files,
                max_strategy_bytes=self.max_strategy_bytes,
                max_model_artifact_files=self.max_model_artifact_files,
                max_model_artifact_bytes=self.max_model_artifact_bytes,
                early_epoch_count=self.early_epoch_count,
                early_max_changed_files=self.early_max_changed_files,
                early_max_diff_lines=self.early_max_diff_lines,
                early_max_code_diff_lines=self.early_max_code_diff_lines,
                is_initial_artifact=self.is_initial_artifact,
            )
        return self

    def evaluate(
        self,
        delta: ModificationDelta,
        model_delta: ModelArtifactDelta | None = None,
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if delta.readonly_violations:
            reasons.append(f"readonly files modified: {list(delta.readonly_violations)}")
        if not self.is_initial_artifact:
            if len(delta.changed_files) > self.max_changed_files:
                reasons.append(f"changed files {len(delta.changed_files)} > {self.max_changed_files}")
            if delta.diff_lines > self.max_diff_lines:
                reasons.append(f"diff lines {delta.diff_lines} > {self.max_diff_lines}")
            if delta.code_diff_lines > self.max_code_diff_lines:
                reasons.append(f"code diff lines {delta.code_diff_lines} > {self.max_code_diff_lines}")
        if delta.total_files > self.max_strategy_files:
            reasons.append(f"strategy files {delta.total_files} > {self.max_strategy_files}")
        if delta.total_bytes > self.max_strategy_bytes:
            reasons.append(f"strategy bytes {delta.total_bytes} > {self.max_strategy_bytes}")
        if model_delta is not None:
            if model_delta.total_files > self.max_model_artifact_files:
                reasons.append(f"model artifact files {model_delta.total_files} > {self.max_model_artifact_files}")
            if model_delta.total_bytes > self.max_model_artifact_bytes:
                reasons.append(f"model artifact bytes {model_delta.total_bytes} > {self.max_model_artifact_bytes}")
        return (not reasons, reasons)

    def to_record(self) -> dict[str, object]:
        return {
            "max_changed_files": self.max_changed_files,
            "max_diff_lines": self.max_diff_lines,
            "max_code_diff_lines": self.max_code_diff_lines,
            "max_strategy_files": self.max_strategy_files,
            "max_strategy_bytes": self.max_strategy_bytes,
            "max_model_artifact_files": self.max_model_artifact_files,
            "max_model_artifact_bytes": self.max_model_artifact_bytes,
            "early_epoch_count": self.early_epoch_count,
            "early_max_changed_files": self.early_max_changed_files,
            "early_max_diff_lines": self.early_max_diff_lines,
            "early_max_code_diff_lines": self.early_max_code_diff_lines,
            "is_initial_artifact": self.is_initial_artifact,
        }

    @classmethod
    def from_record(cls, record: dict[str, object]) -> "ModificationConstraints":
        allowed = set(cls().to_record())
        return cls(**{key: record[key] for key in allowed if key in record})


def defined_function_names(*python_files: Path) -> set[str]:
    names: set[str] = set()
    for path in python_files:
        names.update(_defined_function_names(path))
    return names


def _defined_function_names(main_py: Path) -> set[str]:
    try:
        tree = ast.parse(main_py.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        raise ArtifactError(f"{main_py.name} has a syntax error: {exc}") from exc
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _artifact_files(root: Path, *, reject_runtime_cache: bool = True) -> set[str]:
    if not root.is_dir():
        raise ArtifactError(f"missing artifact directory: {root}")
    return _collect_artifact_files(
        root,
        allowed_suffixes=ALLOWED_SUFFIXES,
        metadata_files=ARTIFACT_METADATA_FILES,
        reject_runtime_cache=reject_runtime_cache,
        label="strategy artifact",
    )


def _model_artifact_files(
    root: Path,
    *,
    reject_runtime_cache: bool = True,
    missing_ok: bool = False,
) -> set[str]:
    if not root.exists():
        if missing_ok:
            return set()
        raise ArtifactError(f"missing model artifact directory: {root}")
    if not root.is_dir():
        raise ArtifactError(f"models must be a directory: {root}")
    return _collect_artifact_files(
        root,
        allowed_suffixes=MODEL_ARTIFACT_ALLOWED_SUFFIXES,
        metadata_files=frozenset(),
        reject_runtime_cache=reject_runtime_cache,
        label="models",
    )


def _collect_artifact_files(
    root: Path,
    *,
    allowed_suffixes: frozenset[str],
    metadata_files: frozenset[str],
    reject_runtime_cache: bool,
    label: str,
) -> set[str]:
    files: set[str] = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        relpath = rel.as_posix()
        if relpath in metadata_files:
            continue
        if path.is_symlink():
            raise ArtifactError(f"{label} must not contain symlinks: {relpath}")
        if _has_hidden_part(rel):
            raise ArtifactError(f"{label} must not contain hidden files or directories: {relpath}")
        if _is_runtime_cache(rel):
            if not reject_runtime_cache:
                continue
            raise ArtifactError(f"{label} must not contain runtime cache files: {relpath}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ArtifactError(f"{label} must contain only regular files: {relpath}")
        if rel.suffix.lower() not in allowed_suffixes:
            raise ArtifactError(f"unsupported {label} file type: {relpath}")
        files.add(relpath)
    return files


def _replace_artifact_root(dest_root: Path) -> None:
    dest_root.mkdir(parents=True, exist_ok=True)
    for child in list(dest_root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _copy_formal_files(source_root: Path, dest_root: Path, relpaths: set[str]) -> None:
    for relpath in relpaths:
        source = source_root / relpath
        target = dest_root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _changed_line_count(before: str, after: str) -> int:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    return sum(1 for line in diff if line[:1] in "+-" and line[:3] not in ("+++", "---"))


def _is_runtime_cache(relpath: str | Path) -> bool:
    path = Path(relpath)
    return any(name in path.parts for name in RUNTIME_CACHE_DIR_NAMES) or path.suffix in RUNTIME_CACHE_SUFFIXES


def _has_hidden_part(relpath: str | Path) -> bool:
    return any(part.startswith(".") for part in Path(relpath).parts)


def _runtime_string_constants(main_py: Path) -> list[str]:
    try:
        tree = ast.parse(main_py.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        # Surface a fixable ArtifactError (as main.py does) rather than a raw
        # SyntaxError, so modification_check reports a clear reason for any helper
        # file (candidate.py / trading.py / ...), not just main.py.
        raise ArtifactError(f"{main_py.name} has a syntax error: {exc}") from exc
    docstring_constants = _docstring_constant_ids(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_constants
    ]


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if not body or not isinstance(body[0], ast.Expr):
                continue
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                docstrings.add(id(value))
    return docstrings


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")
