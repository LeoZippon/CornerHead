"""Host-managed XRay proxy for meta-learning sandbox network access.

The proxy process is host-side only. The Agent sees only non-standard
``AT_PROXY_*`` aliases baked into the Docker container environment; commands
remain direct unless the Agent explicitly maps those aliases to standard proxy
variables for a single shell command.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import secrets
import signal
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

META_XRAY_BIN_ENV = "META_LEARNING_XRAY_BIN"
META_XRAY_CONFIG_PATH_ENV = "META_LEARNING_XRAY_CONFIG_PATH"
META_XRAY_CONFIG_JSON_ENV = "META_LEARNING_XRAY_CONFIG_JSON"
META_XRAY_CONFIG_B64_ENV = "META_LEARNING_XRAY_CONFIG_B64"
DEFAULT_XRAY_CONFIG_ENVS = (
    META_XRAY_CONFIG_PATH_ENV,
    META_XRAY_CONFIG_JSON_ENV,
    META_XRAY_CONFIG_B64_ENV,
    META_XRAY_BIN_ENV,
)


@dataclass(frozen=True)
class ManagedProxySpec:
    """Redacted runtime contract for optional managed XRay startup."""

    enabled: bool = True
    xray_bin: str = "xray"
    config_path_env: str = META_XRAY_CONFIG_PATH_ENV
    config_json_env: str = META_XRAY_CONFIG_JSON_ENV
    config_b64_env: str = META_XRAY_CONFIG_B64_ENV
    default_config_path: str | None = None
    startup_timeout_seconds: float = 15.0
    # Default to host-local only. Bridge-mode callers should pass the Docker
    # bridge host IP (for example 10.10.0.1), not 0.0.0.0.
    listen_host: str = "127.0.0.1"
    container_host: str | None = None
    disabled_status: str = "disabled"

    def to_record(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "xray_bin": Path(self.xray_bin).name,
            "config_envs": {
                "path": self.config_path_env,
                "json": self.config_json_env,
                "base64": self.config_b64_env,
                "binary": META_XRAY_BIN_ENV,
            },
            "default_config_file": Path(self.default_config_path).name if self.default_config_path else None,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "listen_host": self.listen_host,
            "container_host": self.container_host,
            "disabled_status": self.disabled_status,
        }

    def start(self, runtime_dir: str | Path) -> "ManagedProxySession":
        if not self.enabled:
            return ManagedProxySession(record={"enabled": False, "status": self.disabled_status})
        source, raw_config = _load_xray_config(self)
        if raw_config is None:
            return ManagedProxySession(record={"enabled": True, "status": "not_configured"})
        xray_bin = _resolve_xray_bin(self.xray_bin)
        prepared = _prepare_xray_config(raw_config, listen_host=self.listen_host)
        runtime = _prepare_private_runtime_dir(Path(runtime_dir))
        config_path = runtime / "xray.generated.json"
        _write_private_json(config_path, prepared.config)
        process = subprocess.Popen(
            [xray_bin, "run", "-config", str(config_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + float(self.startup_timeout_seconds)
            readiness_host = "127.0.0.1" if self.listen_host in {"0.0.0.0", "::"} else self.listen_host
            _wait_for_tcp(readiness_host, prepared.http_port, deadline, process)
            _wait_for_tcp(readiness_host, prepared.socks_port, deadline, process)
        except Exception:
            _stop_process(process)
            raise
        proxy_host = self.container_host or self.listen_host
        http_url = f"http://{prepared.auth_user}:{prepared.auth_pass}@{proxy_host}:{prepared.http_port}"
        socks_url = f"socks5h://{prepared.auth_user}:{prepared.auth_pass}@{proxy_host}:{prepared.socks_port}"
        env = {
            "HTTP_PROXY": http_url,
            "HTTPS_PROXY": http_url,
            "ALL_PROXY": socks_url,
            "NO_PROXY": os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "localhost,127.0.0.1,::1",
        }
        record = {
            "enabled": True,
            "status": "started",
            "source": source,
            "xray_bin": Path(xray_bin).name,
            "http_port": prepared.http_port,
            "socks_port": prepared.socks_port,
            "listen_host": self.listen_host,
            "container_host": proxy_host,
            "container_env_aliases": ["AT_PROXY_HTTP", "AT_PROXY_HTTPS", "AT_PROXY_ALL", "AT_PROXY_NO_PROXY"],
        }
        return ManagedProxySession(record=record, env=env, process=process)


@dataclass
class ManagedProxySession:
    record: dict[str, object]
    env: dict[str, str] = field(default_factory=dict)
    process: subprocess.Popen[bytes] | None = None

    @contextmanager
    def applied_to_environ(self) -> Iterator[None]:
        previous: dict[str, str | None] = {key: os.environ.get(key) for key in self.env}
        try:
            os.environ.update(self.env)
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def stop(self) -> None:
        if self.process is not None:
            _stop_process(self.process)
            self.process = None


@dataclass(frozen=True)
class _PreparedXRayConfig:
    config: dict[str, object]
    http_port: int
    socks_port: int
    auth_user: str
    auth_pass: str


def _load_xray_config(spec: ManagedProxySpec) -> tuple[str | None, dict[str, object] | None]:
    path_value = os.environ.get(spec.config_path_env, "").strip()
    if path_value:
        path = Path(path_value).expanduser()
        if not path.exists():
            raise RuntimeError(f"managed XRay config path does not exist: {path}")
        _require_private_config_file(path)
        return "path_env", _parse_config_text(path.read_text(encoding="utf-8"))
    json_value = os.environ.get(spec.config_json_env, "").strip()
    if json_value:
        return "json_env", _parse_config_text(json_value)
    b64_value = os.environ.get(spec.config_b64_env, "").strip()
    if b64_value:
        try:
            decoded = base64.b64decode(b64_value).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - convert parser detail to a stable runtime error.
            raise RuntimeError("managed XRay base64 config is invalid") from exc
        return "base64_env", _parse_config_text(decoded)
    if spec.default_config_path:
        default_path = Path(spec.default_config_path).expanduser()
        if default_path.exists():
            _require_private_config_file(default_path)
            return "default_file", _parse_config_text(default_path.read_text(encoding="utf-8"))
    return None, None


def _parse_config_text(text: str) -> dict[str, object]:
    try:
        config = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"managed XRay config is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise RuntimeError("managed XRay config must be a JSON object")
    return config


def _resolve_xray_bin(name: str) -> str:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    path = Path(name).expanduser()
    if path.exists():
        return str(path)
    raise RuntimeError(f"managed XRay binary is not available: {name}")


def _require_private_config_file(path: Path) -> None:
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise RuntimeError(f"managed XRay config file is not readable: {path}") from exc
    if not path.is_file():
        raise RuntimeError(f"managed XRay config path must be a regular file: {path}")
    if stat_result.st_mode & 0o077:
        raise RuntimeError(
            f"managed XRay config file must not be group/world accessible; run chmod 600 {path}"
        )


def _prepare_private_runtime_dir(runtime: Path) -> Path:
    runtime.mkdir(parents=True, exist_ok=True)
    if runtime.is_symlink() or not runtime.is_dir():
        raise RuntimeError(f"managed XRay runtime path must be a private directory: {runtime}")
    runtime.chmod(0o700)
    if runtime.stat().st_mode & 0o077:
        raise RuntimeError(f"managed XRay runtime directory must be private: {runtime}")
    return runtime


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    finally:
        if fd >= 0:
            os.close(fd)


def _prepare_xray_config(config: dict[str, object], *, listen_host: str = "127.0.0.1") -> _PreparedXRayConfig:
    prepared = copy.deepcopy(config)
    original_inbounds = prepared.get("inbounds")
    if not isinstance(original_inbounds, list):
        original_inbounds = []
    http_inbound = _first_inbound(original_inbounds, "http") or {
        "tag": "mq-managed-http",
        "protocol": "http",
        "settings": {},
    }
    socks_inbound = _first_inbound(original_inbounds, "socks") or {
        "tag": "mq-managed-socks",
        "protocol": "socks",
        "settings": {"udp": True},
    }
    http_port = _unused_tcp_port()
    socks_port = _unused_tcp_port(excluding={http_port})
    auth_user = f"u{secrets.token_hex(8)}"
    auth_pass = f"p{secrets.token_hex(24)}"
    prepared["inbounds"] = [
        _proxy_inbound(
            http_inbound,
            listen_host=listen_host,
            port=http_port,
            fallback_tag="mq-managed-http",
            auth_user=auth_user,
            auth_pass=auth_pass,
        ),
        _proxy_inbound(
            socks_inbound,
            listen_host=listen_host,
            port=socks_port,
            fallback_tag="mq-managed-socks",
            auth_user=auth_user,
            auth_pass=auth_pass,
        ),
    ]
    return _PreparedXRayConfig(
        config=prepared,
        http_port=http_port,
        socks_port=socks_port,
        auth_user=auth_user,
        auth_pass=auth_pass,
    )


def _first_inbound(inbounds: list[object], protocol: str) -> dict[str, object] | None:
    for inbound in inbounds:
        if isinstance(inbound, dict) and str(inbound.get("protocol", "")).lower() == protocol:
            return copy.deepcopy(inbound)
    return None


def _proxy_inbound(
    inbound: dict[str, object],
    *,
    listen_host: str,
    port: int,
    fallback_tag: str,
    auth_user: str,
    auth_pass: str,
) -> dict[str, object]:
    result = copy.deepcopy(inbound)
    result["listen"] = listen_host
    result["port"] = port
    result.setdefault("tag", fallback_tag)
    settings = result.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    else:
        settings = copy.deepcopy(settings)
    protocol = str(result.get("protocol", "")).lower()
    settings["accounts"] = [{"user": auth_user, "pass": auth_pass}]
    if protocol == "http":
        settings["allowTransparent"] = False
    elif protocol == "socks":
        settings["auth"] = "password"
    result["settings"] = settings
    return result


def _unused_tcp_port(*, excluding: set[int] | None = None) -> int:
    excluding = excluding or set()
    for _ in range(32):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", 0))
            port = int(sock.getsockname()[1])
        if port not in excluding:
            return port
    raise RuntimeError("failed to allocate a free local proxy port")


def _wait_for_tcp(host: str, port: int, deadline: float, process: subprocess.Popen[bytes]) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("managed XRay exited before its proxy ports became ready")
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"managed XRay proxy port did not become ready: {port}")


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    _signal_process_group_or_process(process, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _signal_process_group_or_process(process, signal.SIGKILL)
        process.wait(timeout=5)


def _signal_process_group_or_process(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        pgid = os.getpgid(process.pid)
    except (AttributeError, ProcessLookupError, OSError):
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except OSError:
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
