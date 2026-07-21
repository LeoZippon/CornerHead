"""Derived sandbox image lifecycle (meta-learning ``sandbox_environment.json``).

Meta-learning may request stable new dependencies for later ordinary Folds by
writing ``workspace/sandbox_environment.json``. This module owns the whole
image-extension domain: request validation, Dockerfile rendering (with a
build-time import smoke test), the ``docker build``, content-addressable image
identity, and best-effort GC of stale derived images. The pipeline only wires
config knobs in and records the result.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path

from autotrade.environment.runtime import utc_now_iso
from autotrade.environment.sandbox import SandboxSpec, resolve_image_identity

SANDBOX_ENVIRONMENT_REQUEST_NAME = "sandbox_environment.json"
SANDBOX_ENVIRONMENT_EXAMPLE_NAME = "sandbox_environment.example.json"
_SANDBOX_ENVIRONMENT_EXAMPLE = {
    "python_packages": [],
    "apt_packages": [],
    "npm_packages": [],
    "reason": (
        "Copy this example to sandbox_environment.json only when later ordinary Folds "
        "need stable new dependencies."
    ),
    "notes": (
        "Do not include shell commands, URLs, tokens, cache paths, local files, "
        "or temporary exploration artifacts."
    ),
}

_PYTHON_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-\[\],<>=!~:+]*$")
_SYSTEM_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
_NPM_PACKAGE_RE = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9_.-]*/)?[A-Za-z0-9][A-Za-z0-9_.-]*(?:@[A-Za-z0-9][A-Za-z0-9_.+~^-]*)?$"
)
_DOCKER_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,200}$")


def write_sandbox_environment_example(workspace: Path) -> Path:
    path = workspace / SANDBOX_ENVIRONMENT_EXAMPLE_NAME
    path.write_text(
        json.dumps(_SANDBOX_ENVIRONMENT_EXAMPLE, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def maybe_rebuild_sandbox_image(
    request_path: Path,
    *,
    base_spec: SandboxSpec,
    experiment_id: str,
    epoch_id: str,
    experiment_dir: Path,
    manifest,
    use_docker: bool,
    rebuild_enabled: bool,
    timeout_seconds: int,
    image_keep: int,
) -> tuple[dict[str, object] | None, SandboxSpec]:
    """Build a derived image from a meta-learning environment request.

    Returns ``(result_record, active_spec)``: the spec switches to the new
    image tag only on a successful build. Every outcome (rejected, skipped,
    timeout, failed, ok) is recorded into the run ``manifest`` before a hard
    failure is raised, so the audit trail survives the fail-fast."""
    if not request_path.exists():
        return None, base_spec
    try:
        request = _load_sandbox_environment_request(request_path)
    except ValueError as exc:
        result = {"status": "rejected", "reason": str(exc), "request_ref": f"/mnt/agent/workspace/{request_path.name}"}
        manifest.update(sandbox_image_update=result)
        raise RuntimeError(f"meta-learning sandbox environment request rejected: {exc}") from exc
    if not _environment_request_has_packages(request):
        result = {"status": "skipped_empty", "request_ref": f"/mnt/agent/workspace/{request_path.name}"}
        manifest.update(sandbox_image_update=result)
        return result, base_spec
    if not use_docker:
        result = {"status": "skipped_local_dev", "request_ref": f"/mnt/agent/workspace/{request_path.name}"}
        manifest.update(sandbox_image_update=result)
        return result, base_spec
    if not rebuild_enabled:
        result = {"status": "disabled", "request_ref": f"/mnt/agent/workspace/{request_path.name}"}
        manifest.update(sandbox_image_update=result)
        return result, base_spec

    build_dir = experiment_dir / "sandbox_images" / epoch_id
    build_dir.mkdir(parents=True, exist_ok=True)
    request_hash = _sandbox_environment_hash(request)
    image = f"{_docker_tag_component(experiment_id)}-{epoch_id}-{request_hash[:12]}"
    image_tag = f"autotrade-sandbox:{image}"
    dockerfile = build_dir / "Dockerfile"
    try:
        dockerfile_text = _render_sandbox_extension_dockerfile(base_spec.image, request)
    except ValueError as exc:
        result = {
            "status": "rejected",
            "reason": str(exc),
            "request_ref": f"/mnt/agent/workspace/{request_path.name}",
            "base_image": base_spec.image,
            "request_hash": request_hash,
        }
        manifest.update(sandbox_image_update=result)
        raise RuntimeError(f"meta-learning sandbox image rebuild rejected: {exc}") from exc
    dockerfile.write_text(dockerfile_text, encoding="utf-8")
    request_copy = build_dir / "sandbox_environment.json"
    request_copy.write_text(json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    command = ["docker", "build", "-t", image_tag, "-f", str(dockerfile), str(build_dir)]
    started_at = utc_now_iso()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        result = {
            "status": "timeout",
            "request_ref": f"/mnt/agent/workspace/{request_path.name}",
            "host_request_ref": str(request_copy),
            "dockerfile_ref": str(dockerfile),
            "base_image": base_spec.image,
            "image": image_tag,
            "request_hash": request_hash,
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "timeout_seconds": timeout_seconds,
            "stdout_tail": str(exc.stdout or "")[-4000:],
            "stderr_tail": str(exc.stderr or "")[-4000:],
        }
        manifest.update(sandbox_image_update=result)
        raise RuntimeError(f"meta-learning sandbox image rebuild timed out: {image_tag}") from exc
    result: dict[str, object] = {
        "status": "ok" if completed.returncode == 0 else "failed",
        "request_ref": f"/mnt/agent/workspace/{request_path.name}",
        "host_request_ref": str(request_copy),
        "dockerfile_ref": str(dockerfile),
        "base_image": base_spec.image,
        "image": image_tag,
        "request_hash": request_hash,
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "returncode": completed.returncode,
        "stdout_tail": str(completed.stdout)[-4000:],
        "stderr_tail": str(completed.stderr)[-4000:],
    }
    active_spec = base_spec
    if completed.returncode == 0:
        active_spec = replace(base_spec, image=image_tag)
        # Content-addressable identity of the freshly built image: the tag
        # alone cannot prove which bits later folds ran on.
        image_id, repo_digests = resolve_image_identity(image_tag)
        result["image_id"] = image_id
        result["image_repo_digests"] = repo_digests
        result["pruned_images"] = _gc_derived_sandbox_images(
            experiment_id, keep=image_keep, keep_image=image_tag
        )
    manifest.update(sandbox_image_update=result)
    if completed.returncode != 0:
        raise RuntimeError(f"meta-learning sandbox image rebuild failed: {image_tag}")
    return result, active_spec


def _gc_derived_sandbox_images(experiment_id: str, *, keep: int, keep_image: str) -> list[str]:
    """Best-effort prune of stale derived images for this experiment, keeping the
    most recent ``keep`` (and always the active one). Docker image GC must never
    fail a build, so all errors are swallowed."""
    if keep <= 0:
        return []
    prefix = f"autotrade-sandbox:{_docker_tag_component(experiment_id)}-"
    try:
        listed = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.CreatedAt}}",
             "autotrade-sandbox"],
            capture_output=True, text=True, timeout=30,
        )
        if listed.returncode != 0:
            return []
        rows: list[tuple[str, str]] = []
        for line in listed.stdout.splitlines():
            if not line.startswith(prefix):
                continue
            tag, _, created = line.partition("\t")
            rows.append((tag, created))
        # Sort newest first by Docker's CreatedAt (lexicographic on the
        # "YYYY-MM-DD HH:MM:SS …" prefix is chronological) rather than trusting
        # `docker images` default order; keep the newest, drop the older tail,
        # never removing the just-built active image.
        rows.sort(key=lambda row: row[1], reverse=True)
        stale = [tag for tag, _ in rows[keep:] if tag != keep_image]
        pruned: list[str] = []
        for tag in stale:
            removed = subprocess.run(
                ["docker", "image", "rm", tag], capture_output=True, text=True, timeout=30
            )
            if removed.returncode == 0:
                pruned.append(tag)
        return pruned
    except (OSError, subprocess.SubprocessError):
        return []


def _load_sandbox_environment_request(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"sandbox_environment.json is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("sandbox_environment.json must be a JSON object")
    allowed = {"python_packages", "apt_packages", "npm_packages", "reason", "notes"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"sandbox_environment.json contains unsupported fields: {unknown}")
    request = {
        "python_packages": _validated_package_list(
            raw.get("python_packages"), field="python_packages", pattern=_PYTHON_PACKAGE_RE, max_items=40
        ),
        "apt_packages": _validated_package_list(
            raw.get("apt_packages"), field="apt_packages", pattern=_SYSTEM_PACKAGE_RE, max_items=30
        ),
        "npm_packages": _validated_package_list(
            raw.get("npm_packages"), field="npm_packages", pattern=_NPM_PACKAGE_RE, max_items=30
        ),
    }
    for key in ("reason", "notes"):
        value = raw.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            request[key] = value[:2000]
    return request


def _validated_package_list(
    value: object,
    *,
    field: str,
    pattern: re.Pattern[str],
    max_items: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    packages: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} entries must be non-empty strings")
        package = item.strip()
        if package.startswith("-") or not pattern.match(package):
            raise ValueError(f"unsupported {field} entry: {package!r}")
        if package not in packages:
            packages.append(package)
    if len(packages) > max_items:
        raise ValueError(f"{field} has {len(packages)} entries > {max_items}")
    return packages


def _environment_request_has_packages(request: dict[str, object]) -> bool:
    return any(request.get(key) for key in ("python_packages", "apt_packages", "npm_packages"))


def _sandbox_environment_hash(request: dict[str, object]) -> str:
    payload = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _docker_tag_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return (cleaned or "experiment")[:48].lower()


def _render_sandbox_extension_dockerfile(base_image: str, request: dict[str, object]) -> str:
    if not _DOCKER_IMAGE_RE.match(base_image):
        raise ValueError(f"unsupported base sandbox image: {base_image!r}")
    lines = [
        "# Generated by AutoTrade Pipeline from meta-learning sandbox_environment.json.",
        f"FROM {base_image}",
        "ARG PIP_INDEX_URL=https://pypi.org/simple",
        "USER root",
    ]
    apt_packages = [shlex.quote(item) for item in request.get("apt_packages", [])]
    if apt_packages:
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            + " ".join(apt_packages)
            + " && rm -rf /var/lib/apt/lists/*"
        )
    python_specs = list(request.get("python_packages", []))
    python_packages = [shlex.quote(item) for item in python_specs]
    if python_packages:
        lines.append(
            'RUN python -m pip install --no-cache-dir -i "${PIP_INDEX_URL}" '
            + " ".join(python_packages)
        )
        # Verification layer: a build that installs a package but cannot import it
        # is a silent transfer failure for later Folds. Fail the build here so
        # "image built" implies "importable", not just "installable".
        imports = _python_import_names(python_specs)
        if imports:
            stmt = "; ".join(f"import {name}" for name in imports)
            lines.append(f'RUN python -c {shlex.quote(stmt)}')
    npm_packages = [shlex.quote(item) for item in request.get("npm_packages", [])]
    if npm_packages:
        lines.append("RUN npm install -g --no-fund --no-audit " + " ".join(npm_packages))
    lines.extend(["WORKDIR /mnt/agent", ""])
    return "\n".join(lines)


# PyPI distribution name -> import module name for the cases where they diverge.
_IMPORT_NAME_ALIASES = {
    "scikit-learn": "sklearn",
    "opencv-python": "cv2",
    "opencv-contrib-python": "cv2",
    "umap-learn": "umap",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
    "msgpack-python": "msgpack",
    "faiss-cpu": "faiss",
    "faiss-gpu": "faiss",
}


def _python_import_names(specs: list[str]) -> list[str]:
    """Top-level import names for declared python_packages, for a build-time smoke
    test. Only emit a name we are confident about: a known alias, or a simple
    distribution name with no '-'/'.' (where dist == import). For a hyphenated/dotted
    name that is not aliased the import module is unguessable (e.g. umap-learn->umap,
    opencv-contrib-python->cv2), so we SKIP its smoke import rather than reject a
    validly-installed package; the build still verifies pip install succeeded."""
    names: list[str] = []
    for spec in specs:
        dist = re.split(r"[<>=!~;\[\s]", str(spec).strip(), maxsplit=1)[0].strip()
        if not dist:
            continue
        lower = dist.lower()
        if lower in _IMPORT_NAME_ALIASES:
            module = _IMPORT_NAME_ALIASES[lower]
        elif "-" in lower or "." in lower:
            continue  # ambiguous import name — rely on pip install success
        else:
            module = lower
        if module and module.isidentifier() and module not in names:
            names.append(module)
    return names
