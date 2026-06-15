"""Strategy artifact contract: factor/ and nl_prior/ formats, hashing, and diffs.

The strategy artifact is the only content passed between Folds/Epochs. It is the
pair of directories `factor/` and `nl_prior/` described in docs/agent_design.md
section 4 and checked by `modification_check_tool` in docs/environment_design.md
section 4.3.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

STRATEGY_DIRS = ("factor", "nl_prior")
FACTOR_REQUIRED_FILES = ("README.md", "main.py", "factors.json")
PRIOR_REQUIRED_FILES = ("README.md", "prior.json")
AGENT_WRITABLE_FILES = frozenset({"factor/main.py", "factor/factors.json", "nl_prior/prior.json"})
READONLY_FILES = frozenset({"factor/README.md", "nl_prior/README.md"})
FACTOR_FIELDS = ("id", "function", "description", "lookback_days", "direction", "rationale")
RULE_FIELDS = ("id", "text", "evidence", "effect")
# Formal strategy code must read /mnt/snapshot only; stage directories are
# exploration-only (docs/environment_design.md 4.4).
FORBIDDEN_CODE_REFERENCES = ("/mnt/snapshots/", "/mnt/runtime/")


class ArtifactError(ValueError):
    """A strategy artifact violates the documented format contract."""


@dataclass(frozen=True)
class StrategyArtifact:
    root: Path
    factors: tuple[dict[str, object], ...]
    rules: tuple[dict[str, object], ...]
    artifact_hash: str


def validate_factors_payload(payload: object) -> tuple[dict[str, object], ...]:
    if not isinstance(payload, dict) or set(payload) != {"factors"} or not isinstance(payload["factors"], list):
        raise ArtifactError('factors.json must be {"factors": [...]} with no extra top-level keys')
    factors: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in payload["factors"]:
        if not isinstance(entry, dict):
            raise ArtifactError("each factor entry must be an object")
        missing = [key for key in FACTOR_FIELDS if key not in entry]
        if missing:
            raise ArtifactError(f"factor entry missing fields: {missing}")
        factor_id = str(entry["id"]).strip()
        if not factor_id or factor_id in seen:
            raise ArtifactError(f"factor id must be unique and non-empty: {entry['id']!r}")
        if not str(entry["function"]).strip():
            raise ArtifactError(f"factor {factor_id} has empty function name")
        if not isinstance(entry["lookback_days"], int) or entry["lookback_days"] < 0:
            raise ArtifactError(f"factor {factor_id} lookback_days must be a non-negative integer")
        if not str(entry["rationale"]).strip():
            raise ArtifactError(f"factor {factor_id} must state a rationale for its introduction")
        seen.add(factor_id)
        factors.append(entry)
    return tuple(factors)


def validate_prior_payload(payload: object) -> tuple[dict[str, object], ...]:
    if not isinstance(payload, dict) or set(payload) != {"rules"} or not isinstance(payload["rules"], list):
        raise ArtifactError('prior.json must be {"rules": [...]} with no extra top-level keys')
    rules: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in payload["rules"]:
        if not isinstance(entry, dict):
            raise ArtifactError("each prior rule must be an object")
        missing = [key for key in RULE_FIELDS if key not in entry]
        if missing:
            raise ArtifactError(f"prior rule missing fields: {missing}")
        rule_id = str(entry["id"]).strip()
        if not rule_id or rule_id in seen:
            raise ArtifactError(f"rule id must be unique and non-empty: {entry['id']!r}")
        for key in ("text", "evidence", "effect"):
            if not str(entry[key]).strip():
                raise ArtifactError(f"rule {rule_id} has empty {key}")
        seen.add(rule_id)
        rules.append(entry)
    return tuple(rules)


def defined_function_names(main_py: Path) -> set[str]:
    try:
        tree = ast.parse(main_py.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        raise ArtifactError(f"factor/main.py has a syntax error: {exc}") from exc
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def load_strategy_artifact(root: str | Path) -> StrategyArtifact:
    """Load and validate a factor/ + nl_prior/ artifact directory pair."""
    root = Path(root)
    _artifact_files(root)
    for sub, required in (("factor", FACTOR_REQUIRED_FILES), ("nl_prior", PRIOR_REQUIRED_FILES)):
        for name in required:
            if not (root / sub / name).is_file():
                raise ArtifactError(f"missing required artifact file: {sub}/{name}")
    factors = validate_factors_payload(_load_json(root / "factor" / "factors.json"))
    rules = validate_prior_payload(_load_json(root / "nl_prior" / "prior.json"))
    main_py = root / "factor" / "main.py"
    functions = defined_function_names(main_py)
    if "generate_candidates" not in functions:
        raise ArtifactError("factor/main.py must define generate_candidates()")
    for factor in factors:
        if str(factor["function"]) not in functions:
            raise ArtifactError(f"registered factor function not found in main.py: {factor['function']}")
    for needle in FORBIDDEN_CODE_REFERENCES:
        if any(needle in value for value in _runtime_string_constants(main_py)):
            raise ArtifactError(f"formal strategy code must not reference stage directories: {needle}")
    return StrategyArtifact(root=root, factors=factors, rules=rules, artifact_hash=artifact_hash(root))


def artifact_hash(root: str | Path) -> str:
    """Deterministic aggregate hash over all files in factor/ and nl_prior/."""
    root = Path(root)
    digest = hashlib.sha256()
    for relpath in sorted(_artifact_files(root)):
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\x00")
        digest.update((root / relpath).read_bytes())
        digest.update(b"\x00")
    return f"sha256:{digest.hexdigest()}"


def init_from_template(template_dir: str | Path, dest_root: str | Path) -> None:
    """Initialize agent_output/ from configs/agent_output_template/."""
    template_dir = Path(template_dir)
    dest_root = Path(dest_root)
    for sub in STRATEGY_DIRS:
        source = template_dir / sub
        if not source.is_dir():
            raise ArtifactError(f"template missing directory: {source}")
        shutil.copytree(source, dest_root / sub, dirs_exist_ok=True)


def copy_artifact(source_root: str | Path, dest_root: str | Path) -> None:
    """Copy the factor/ and nl_prior/ pair, replacing any existing copies."""
    source_root = Path(source_root)
    dest_root = Path(dest_root)
    _artifact_files(source_root)
    for sub in STRATEGY_DIRS:
        target = dest_root / sub
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_root / sub, target)


@dataclass(frozen=True)
class ModificationDelta:
    changed_files: tuple[str, ...]
    diff_lines: int
    factors_added: tuple[str, ...]
    factors_removed: tuple[str, ...]
    factors_modified: tuple[str, ...]
    rules_added: tuple[str, ...]
    rules_removed: tuple[str, ...]
    rules_rewritten: tuple[str, ...]
    total_factors: int
    total_rules: int
    max_rule_text_chars: int
    readonly_violations: tuple[str, ...]

    @property
    def factor_changes(self) -> int:
        return len(self.factors_added) + len(self.factors_removed) + len(self.factors_modified)

    @property
    def rule_changes(self) -> int:
        return len(self.rules_added) + len(self.rules_removed) + len(self.rules_rewritten)

    def to_record(self) -> dict[str, object]:
        return {
            "changed_files": list(self.changed_files),
            "changed_file_count": len(self.changed_files),
            "diff_lines": self.diff_lines,
            "factors_added": list(self.factors_added),
            "factors_removed": list(self.factors_removed),
            "factors_modified": list(self.factors_modified),
            "rules_added": list(self.rules_added),
            "rules_removed": list(self.rules_removed),
            "rules_rewritten": list(self.rules_rewritten),
            "total_factors": self.total_factors,
            "total_rules": self.total_rules,
            "max_rule_text_chars": self.max_rule_text_chars,
            "readonly_violations": list(self.readonly_violations),
        }


def modification_delta(parent_root: str | Path, work_root: str | Path) -> ModificationDelta:
    """Deterministic counts of work copy changes relative to the parent copy."""
    parent_root = Path(parent_root)
    work_root = Path(work_root)
    parent_files = _artifact_files(parent_root)
    work_files = _artifact_files(work_root)
    changed: list[str] = []
    diff_lines = 0
    readonly_violations: list[str] = []
    for relpath in sorted(parent_files | work_files):
        parent_text = _read_text(parent_root / relpath) if relpath in parent_files else None
        work_text = _read_text(work_root / relpath) if relpath in work_files else None
        if parent_text == work_text:
            continue
        changed.append(relpath)
        if relpath in READONLY_FILES:
            readonly_violations.append(relpath)
        diff = difflib.unified_diff(
            (parent_text or "").splitlines(), (work_text or "").splitlines(), lineterm="", n=0
        )
        diff_lines += sum(1 for line in diff if line[:1] in "+-" and line[:3] not in ("+++", "---"))

    parent_factors = _entries_by_id(parent_root / "factor" / "factors.json", "factors")
    work_factors = _entries_by_id(work_root / "factor" / "factors.json", "factors")
    parent_rules = _entries_by_id(parent_root / "nl_prior" / "prior.json", "rules")
    work_rules = _entries_by_id(work_root / "nl_prior" / "prior.json", "rules")
    return ModificationDelta(
        changed_files=tuple(changed),
        diff_lines=diff_lines,
        factors_added=_ids_added(parent_factors, work_factors),
        factors_removed=_ids_added(work_factors, parent_factors),
        factors_modified=_ids_modified(parent_factors, work_factors),
        rules_added=_ids_added(parent_rules, work_rules),
        rules_removed=_ids_added(work_rules, parent_rules),
        rules_rewritten=_ids_modified(parent_rules, work_rules),
        total_factors=len(work_factors),
        total_rules=len(work_rules),
        max_rule_text_chars=max((len(str(rule.get("text", ""))) for rule in work_rules.values()), default=0),
        readonly_violations=tuple(readonly_violations),
    )


@dataclass(frozen=True)
class ModificationConstraints:
    """Per-Step/Fold limits handed down by the Pipeline via the run manifest."""

    max_changed_files: int = 4
    max_diff_lines: int = 200
    max_factor_changes: int = 4
    max_rule_changes: int = 4
    max_total_factors: int = 12
    max_total_rules: int = 12
    max_rule_text_chars: int = 400
    is_initial_artifact: bool = False

    def evaluate(self, delta: ModificationDelta) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if delta.readonly_violations:
            reasons.append(f"readonly files modified: {list(delta.readonly_violations)}")
        if not self.is_initial_artifact:
            if len(delta.changed_files) > self.max_changed_files:
                reasons.append(f"changed files {len(delta.changed_files)} > {self.max_changed_files}")
            if delta.diff_lines > self.max_diff_lines:
                reasons.append(f"diff lines {delta.diff_lines} > {self.max_diff_lines}")
            if delta.factor_changes > self.max_factor_changes:
                reasons.append(f"factor changes {delta.factor_changes} > {self.max_factor_changes}")
            if delta.rule_changes > self.max_rule_changes:
                reasons.append(f"rule changes {delta.rule_changes} > {self.max_rule_changes}")
        if delta.total_factors > self.max_total_factors:
            reasons.append(f"total factors {delta.total_factors} > {self.max_total_factors}")
        if delta.total_rules > self.max_total_rules:
            reasons.append(f"total rules {delta.total_rules} > {self.max_total_rules}")
        if delta.max_rule_text_chars > self.max_rule_text_chars:
            reasons.append(f"rule text length {delta.max_rule_text_chars} > {self.max_rule_text_chars}")
        return (not reasons, reasons)

    def to_record(self) -> dict[str, object]:
        return {
            "max_changed_files": self.max_changed_files,
            "max_diff_lines": self.max_diff_lines,
            "max_factor_changes": self.max_factor_changes,
            "max_rule_changes": self.max_rule_changes,
            "max_total_factors": self.max_total_factors,
            "max_total_rules": self.max_total_rules,
            "max_rule_text_chars": self.max_rule_text_chars,
            "is_initial_artifact": self.is_initial_artifact,
        }

    @classmethod
    def from_record(cls, record: dict[str, object]) -> "ModificationConstraints":
        return cls(**{key: record[key] for key in cls().to_record() if key in record})


def _artifact_files(root: Path) -> set[str]:
    files: set[str] = set()
    for sub in STRATEGY_DIRS:
        base = root / sub
        if not base.is_dir():
            raise ArtifactError(f"missing artifact directory: {base}")
        for path in base.rglob("*"):
            relpath = str(path.relative_to(root))
            if path.is_symlink():
                raise ArtifactError(f"strategy artifact must not contain symlinks: {relpath}")
            if not path.is_file() and not path.is_dir():
                raise ArtifactError(f"strategy artifact must contain only regular files/directories: {relpath}")
            # Runtime caches are not part of the artifact contract or its hash.
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
                files.add(relpath)
    return files


def _runtime_string_constants(main_py: Path) -> list[str]:
    """String constants that can reach runtime; comments and docstrings excluded."""
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{path.name} is not valid JSON: {exc}") from exc


def _entries_by_id(path: Path, key: str) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    payload = _load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise ArtifactError(f"{path} must contain a top-level {key} list")
    return {str(entry.get("id", "")): entry for entry in payload[key] if isinstance(entry, dict)}


def _ids_added(before: dict[str, dict[str, object]], after: dict[str, dict[str, object]]) -> tuple[str, ...]:
    return tuple(sorted(set(after) - set(before)))


def _ids_modified(before: dict[str, dict[str, object]], after: dict[str, dict[str, object]]) -> tuple[str, ...]:
    return tuple(sorted(key for key in set(before) & set(after) if before[key] != after[key]))
