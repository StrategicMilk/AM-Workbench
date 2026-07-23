"""Single-owner, on-demand lifecycle supervisor for the vendored AM Engine."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx as httpx

from vetinari.constants import get_user_dir
from vetinari.engine import supervisor_security
from vetinari.engine.binary import (
    ENGINE_BINARY_ENV,
    EXPECTED_ENGINE_VERSION,
)
from vetinari.engine.binary import (
    probe_version as probe_version,
)
from vetinari.engine.binary import (
    provision_binary as provision_binary,
)
from vetinari.engine.binary import (
    resolve_binary as resolve_binary,
)
from vetinari.engine.supervisor_events import EngineEventsMixin
from vetinari.engine.supervisor_lifecycle import EngineLifecycleMixin
from vetinari.engine.supervisor_protocol import EngineProtocolMixin
from vetinari.exceptions import EngineUnavailableError

if TYPE_CHECKING:
    from vetinari.engine.events import EventIngester, EventsClient
    from vetinari.engine.trust_anchor import EngineTrustAnchor

__all__ = [
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "DEFAULT_KEEP_ALIVE",
    "DEFAULT_MAX_RESTART_ATTEMPTS",
    "DEFAULT_RESTART_RESET_SECONDS",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "ENGINE_CONFIG_RENDER_TABLE",
    "EngineCapabilities",
    "EngineConfig",
    "EngineEndpoint",
    "EngineRuntimeMode",
    "EngineState",
    "EngineStatus",
    "EngineSupervisor",
    "parse_keep_alive",
    "render_engine_config",
    "write_engine_config",
]

DEFAULT_MAX_RESTART_ATTEMPTS = 5
DEFAULT_RESTART_RESET_SECONDS = 60.0
DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0
DEFAULT_KEEP_ALIVE = "30m"
ENGINE_RUNTIME_SUBDIR = "engine/runtime"
_RUNTIME_FILE_NAMES = ("supervisor.json", "auth.token")
_AUTH_POLICY_FILE_NAME = "auth-policy.json"
_LOCAL_SUPERVISOR_PRINCIPAL = "local-supervisor"
_MAX_AUTH_TOKEN_BYTES = 512
_STARTUP_LOG_MAX_LINES = 64
_STARTUP_LOG_MAX_CHARS = 16 * 1024
_STARTUP_LOG_LINE_MAX_CHARS = 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

ENGINE_CONFIG_RENDER_TABLE: tuple[tuple[str, str], ...] = (
    ("host", "server.host"),
    ("port", "server.port"),
    ("auth_token_path", "server.auth_token_path"),
    ("auth_policy_path", "server.auth_policy_path"),
    ("model_dirs", "models.dirs"),
    ("vram_budget_gb", "budgets.vram_gb"),
    ("ram_budget_gb", "budgets.ram_gb"),
    ("budget_margin_pct", "budgets.margin_pct"),
    ("parallel_requests", "slots.count"),
    ("context_size", "slots.default_ctx"),
    ("kv_cache_type_k", "kv.cache_type_k"),
    ("kv_cache_type_v", "kv.cache_type_v"),
    ("session_dir", "kv.session_dir"),
    ("keep_alive", "idle.keep_alive"),
    ("preemption", "scheduler.preemption"),
    ("batch_token_budget", "scheduler.batch_token_budget"),
    ("log_level", "log.level"),
    ("log_dir", "log.dir"),
)


class EngineState(str, Enum):
    """Observable lifecycle state for the owned engine process."""

    STOPPED = "stopped"
    PROVISIONING = "provisioning"
    STARTING = "starting"
    DRAINING = "draining"
    RUNNING = "running"
    DEGRADED = "degraded"
    VERSION_MISMATCH = "version-mismatch"


class EngineRuntimeMode(str, Enum):
    """HTTP control-plane contract implemented by the configured engine binary."""

    SCAFFOLD = "scaffold"
    OWNED = "owned"


@dataclass(frozen=True, slots=True)
class EngineCapabilities:
    """Compatibility flags for optional administrative surfaces."""

    inference: bool = True
    health: bool = True
    version: bool = False
    model_unload: bool = True
    config_reload: bool = False
    admin_shutdown: bool = False

    def __repr__(self) -> str:
        """Return capability availability without runtime secrets."""
        return (
            "EngineCapabilities("
            f"inference={self.inference!r}, model_unload={self.model_unload!r}, "
            f"config_reload={self.config_reload!r})"
        )


@dataclass(frozen=True, slots=True)
class EngineEndpoint:
    """Connection material shared with a second Vetinari process."""

    pid: int
    host: str
    port: int
    token_path: Path
    generation: int = 0

    @property
    def url(self) -> str:
        """Return the HTTP origin for the engine."""
        return f"http://{self.host}:{self.port}"

    def __repr__(self) -> str:
        """Return connection metadata without reading or exposing the token."""
        return (
            f"EngineEndpoint(pid={self.pid!r}, host={self.host!r}, port={self.port!r}, generation={self.generation!r})"
        )


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Runtime policy and the complete first-party EngineConfig boundary."""

    binary_path: Path | None = None
    model_path: Path | None = None
    runtime_mode: EngineRuntimeMode = EngineRuntimeMode.OWNED
    host: str = "127.0.0.1"
    port: int = 0
    auth_token_path: Path | None = None
    auth_policy_path: Path | None = None
    receipt_trust_anchor_path: Path | None = None
    receipt_anchor_sha256: str | None = None
    receipt_authority_pin_sha256: str | None = None
    receipt_ledger_path: Path | None = None
    model_dirs: tuple[Path, ...] = ()
    expected_version: str = EXPECTED_ENGINE_VERSION
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS
    request_timeout_seconds: float = 2.0
    max_restart_attempts: int = DEFAULT_MAX_RESTART_ATTEMPTS
    restart_base_delay_seconds: float = 0.5
    restart_reset_seconds: float = DEFAULT_RESTART_RESET_SECONDS
    drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS
    keep_alive: str = DEFAULT_KEEP_ALIVE
    context_size: int = 8192
    gpu_layers: int = -1
    parallel_requests: int = 1
    vram_budget_gb: float = 0.0
    ram_budget_gb: float = 30.0
    budget_margin_pct: float = 10.0
    kv_cache_type_k: str = "f16"
    kv_cache_type_v: str = "f16"
    session_dir: Path | None = None
    preemption: bool = True
    batch_token_budget: int = 2048
    log_level: str = "info"
    log_dir: Path | None = None
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_mode, EngineRuntimeMode):
            raise ValueError("runtime_mode must be an EngineRuntimeMode")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("engine host must be a loopback address")
        if not 0 <= self.port <= 65535:
            raise ValueError("engine port must be between 0 and 65535")
        if self.max_restart_attempts < 0:
            raise ValueError("max_restart_attempts must be non-negative")
        if self.startup_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ValueError("engine timeouts must be positive")
        if self.drain_timeout_seconds <= 0 or self.restart_base_delay_seconds < 0 or self.restart_reset_seconds <= 0:
            raise ValueError("drain and restart reset timeouts must be positive and restart delay non-negative")
        if self.context_size <= 0 or self.parallel_requests <= 0 or self.batch_token_budget <= 0:
            raise ValueError("engine context, slot count, and batch token budget must be positive")
        if any(not math.isfinite(value) or value < 0 for value in self._memory_budget_values()):
            raise ValueError("engine memory budgets and margin must be finite and non-negative")
        if self.budget_margin_pct > 100:
            raise ValueError("engine budget margin cannot exceed 100 percent")
        if self.kv_cache_type_k not in {"f16", "q8_0", "q4_0"}:
            raise ValueError("kv_cache_type_k must be f16, q8_0, or q4_0")
        if self.kv_cache_type_v not in {"f16", "q8_0", "q4_0"}:
            raise ValueError("kv_cache_type_v must be f16, q8_0, or q4_0")
        if self.log_level not in {"trace", "debug", "info", "warn", "error"}:
            raise ValueError("engine log_level must be trace, debug, info, warn, or error")
        receipt_trust_values = (
            self.receipt_trust_anchor_path,
            self.receipt_anchor_sha256,
            self.receipt_authority_pin_sha256,
            self.receipt_ledger_path,
        )
        if any(value is not None for value in receipt_trust_values) and not all(
            value is not None for value in receipt_trust_values
        ):
            raise ValueError(
                "engine receipt trust requires anchor path, exact anchor SHA-256, "
                "authority pin SHA-256, and ledger path together"
            )
        if all(value is not None for value in receipt_trust_values):
            if self.runtime_mode is not EngineRuntimeMode.OWNED:
                raise ValueError("engine receipt trust is available only for the owned runtime")
            if self.binary_path is not None:
                raise ValueError("engine receipt trust is unavailable for an arbitrary binary override")
            if not isinstance(self.receipt_trust_anchor_path, Path) or not isinstance(self.receipt_ledger_path, Path):
                raise ValueError("engine receipt trust anchor and ledger must be pathlib.Path values")
            if not self.receipt_trust_anchor_path.is_absolute() or not self.receipt_ledger_path.is_absolute():
                raise ValueError("engine receipt trust anchor and ledger paths must be absolute")
            for label, digest in (
                ("anchor", self.receipt_anchor_sha256),
                ("authority pin", self.receipt_authority_pin_sha256),
            ):
                if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                    raise ValueError(f"engine receipt {label} SHA-256 must be 64 lowercase hexadecimal characters")
        parse_keep_alive(self.keep_alive)

    @property
    def receipt_trust_provisioned(self) -> bool:
        """Return whether the complete independently pinned receipt trust set is configured."""
        return self.receipt_trust_anchor_path is not None

    def _memory_budget_values(self) -> tuple[float, float, float]:
        return self.vram_budget_gb, self.ram_budget_gb, self.budget_margin_pct

    def __repr__(self) -> str:
        """Return the lifecycle policy without environment or credential data."""
        return (
            "EngineConfig("
            f"runtime_mode={self.runtime_mode.value!r}, host={self.host!r}, "
            f"expected_version={self.expected_version!r}, "
            f"max_restart_attempts={self.max_restart_attempts!r}, keep_alive={self.keep_alive!r})"
        )


@dataclass(frozen=True, slots=True)
class EngineStatus:
    """Read-only health snapshot surfaced to monitors and operators."""

    state: EngineState
    healthy: bool
    endpoint: str | None
    pid: int | None
    restart_attempts: int
    user_message: str
    capabilities: EngineCapabilities
    startup_log_tail: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact operator-safe lifecycle summary."""
        return (
            "EngineStatus("
            f"state={self.state!r}, healthy={self.healthy!r}, pid={self.pid!r}, "
            f"restart_attempts={self.restart_attempts!r})"
        )


def parse_keep_alive(value: str | int) -> float | None:
    """Parse duration, ``0`` (unload), or ``-1`` (pin) into seconds.

    Returns:
        Seconds, ``0.0`` for immediate unload, or ``None`` for a pin.

    Raises:
        ValueError: If the value is outside the supported duration grammar.
    """
    if isinstance(value, int):
        if value == -1:
            return None
        if value == 0:
            return 0.0
        raise ValueError("integer keep_alive must be 0 or -1")
    text = str(value).strip().lower()
    if text == "-1":
        return None
    if text == "0":
        return 0.0
    if len(text) < 2 or text[-1] not in {"s", "m", "h"}:
        raise ValueError("keep_alive must be a duration such as 30m, 0, or -1")
    try:
        amount = float(text[:-1])
    except ValueError as exc:
        raise ValueError("keep_alive duration is invalid") from exc
    if amount < 0:
        raise ValueError("keep_alive duration cannot be negative")
    return amount * {"s": 1.0, "m": 60.0, "h": 3600.0}[text[-1]]


def _toml_literal(value: object) -> str:
    value = value.value if isinstance(value, Enum) else value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def render_engine_config(
    config: EngineConfig,
    *,
    runtime_dir: Path | None = None,
    port: int | None = None,
    auth_token_path: Path | None = None,
    auth_policy_path: Path | None = None,
) -> str:
    """Render the exact first-party EngineConfig schema as deterministic TOML.

    Returns:
        Stable TOML text accepted by the first-party engine loader.
    """
    base = (runtime_dir or Path.cwd()).resolve()
    selected_model_dirs = config.model_dirs
    if not selected_model_dirs and config.model_path is not None:
        selected_model_dirs = (config.model_path.expanduser().resolve().parent,)
    if not selected_model_dirs:
        selected_model_dirs = (base / "models",)
    render_values: dict[str, object] = {
        "port": config.port if port is None else port,
        "auth_token_path": auth_token_path or config.auth_token_path or (base / "auth.token"),
        "auth_policy_path": auth_policy_path or config.auth_policy_path or (base / _AUTH_POLICY_FILE_NAME),
        "model_dirs": tuple(path.expanduser().resolve() for path in selected_model_dirs),
        "session_dir": config.session_dir or (base / "sessions"),
        "log_dir": config.log_dir or (base / "logs"),
    }
    sections: dict[str, list[tuple[str, object]]] = {}
    for attribute, dotted_key in ENGINE_CONFIG_RENDER_TABLE:
        section, key = dotted_key.split(".", maxsplit=1)
        sections.setdefault(section, []).append((key, render_values.get(attribute, getattr(config, attribute))))
    lines: list[str] = []
    for section, section_values in sections.items():
        if lines:
            lines.append("")
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {_toml_literal(value)}" for key, value in section_values)
    return "\n".join(lines) + "\n"


def write_engine_config(
    config: EngineConfig,
    path: Path,
    *,
    port: int | None = None,
    auth_token_path: Path | None = None,
    auth_policy_path: Path | None = None,
) -> Path:
    """Atomically write the deterministic rendered config.

    Args:
        config: Reviewed engine configuration.
        path: Destination TOML path.
        port: Optional listener-port override.
        auth_token_path: Optional generated token-path override.
        auth_policy_path: Optional generated policy-path override.

    Returns:
        Destination path after atomic replacement.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(f"{path.suffix}.tmp")
    staged.write_text(
        render_engine_config(
            config,
            runtime_dir=path.parent,
            port=port,
            auth_token_path=auth_token_path,
            auth_policy_path=auth_policy_path,
        ),
        encoding="utf-8",
    )
    os.replace(staged, path)
    return path


def _write_private_text(path: Path, content: str) -> None:
    staged = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    replaced = False
    try:
        _secure_private_path(staged, directory=False)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        replaced = True
        _secure_private_path(path, directory=False)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            staged.unlink()
        if replaced:
            with suppress(OSError):
                path.unlink()
        raise EngineUnavailableError(
            "AM Engine private runtime file could not be secured",
            path=str(path),
        ) from exc


def _secure_private_path(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        _secure_windows_path(path, directory=directory)
        return
    supervisor_security.secure_private_path(path, directory=directory)


_secure_windows_path = supervisor_security._secure_windows_path


class EngineSupervisor(EngineLifecycleMixin, EngineEventsMixin, EngineProtocolMixin):
    """Own the sole vendored engine child and fail closed on handshake errors."""

    def __init__(
        self,
        config: EngineConfig | None = None,
        *,
        runtime_dir: Path | None = None,
        binary_resolver: Callable[[], Path] | None = None,
        process_factory: Callable[..., subprocess.Popen[str]] | None = None,
        request_json: Callable[[str, str, str, float, str, Mapping[str, Any] | None], Mapping[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        pid_alive: Callable[[int], bool] | None = None,
        event_ingester_factory: Callable[[], EventIngester] | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.runtime_dir = runtime_dir or (get_user_dir() / ENGINE_RUNTIME_SUBDIR)
        self.pidfile_path = self.runtime_dir / _RUNTIME_FILE_NAMES[0]
        self.token_path = self.runtime_dir / _RUNTIME_FILE_NAMES[1]
        self.config_path = self.runtime_dir / "engine.toml"
        self.auth_policy_path = self.runtime_dir / _AUTH_POLICY_FILE_NAME
        self._binary_resolver = binary_resolver or self._resolve_configured_binary
        self._process_factory = process_factory or subprocess.Popen
        self._request_json = request_json or self._default_request_json
        self._sleep = sleep
        self._monotonic = monotonic
        self._pid_alive = pid_alive or _pid_is_alive
        self._lock = threading.RLock()
        self._lifecycle_transition_lock = threading.RLock()
        self._restart_condition = threading.Condition(self._lock)
        self._restart_in_progress = False
        self._restart_epoch = 0
        self._process: subprocess.Popen[str] | None = None
        self._endpoint: EngineEndpoint | None = None
        self._endpoint_generation = 0
        self._receipt_trust_identity: object | None = None
        self._receipt_engine_instance_id: str | None = None
        self._owner_receipt_trust_identity: object | None = None
        self._configured_receipt_trust_anchor: EngineTrustAnchor | None = None
        self._uses_default_binary_resolver = binary_resolver is None
        self._model_bootstrapped_generation: int | None = None
        self._state = EngineState.STOPPED
        self._restart_attempts = 0
        self._restart_exhausted = False
        self._provision_in_progress = False
        self._process_started_at: float | None = None
        self._user_message = ""
        self._last_activity = self._monotonic()
        self._suspended = False
        self._factory_pin_count = 0
        from vetinari.engine.events import EventIngester

        self._event_ingester_factory = event_ingester_factory or EventIngester
        self._event_ingester = self._event_ingester_factory()
        self._event_ingester_started = False
        self._events_transition_lock = threading.RLock()
        self._events_loop: asyncio.AbstractEventLoop | None = None
        self._events_thread: threading.Thread | None = None
        self._events_ready = threading.Event()
        self._events_finished = threading.Event()
        self._events_client: EventsClient | None = None
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop: threading.Event | None = None
        self._startup_log_lock = threading.Lock()
        self._startup_log_epoch = 0
        self._startup_log_tail: deque[str] = deque(maxlen=_STARTUP_LOG_MAX_LINES)
        self._startup_log_chars = 0
        self._startup_log_thread: threading.Thread | None = None
        self.capabilities = EngineCapabilities(version=self.config.runtime_mode is EngineRuntimeMode.OWNED)

    @property
    def state(self) -> EngineState:
        """Return the current lifecycle state."""
        with self._lock:
            return self._state

    @property
    def endpoint(self) -> EngineEndpoint | None:
        """Return current connection metadata when known."""
        with self._lock:
            return self._endpoint

    @property
    def endpoint_generation(self) -> int:
        """Return the last atomically published endpoint generation."""
        with self._lock:
            return self._endpoint_generation

    def receipt_trust_context(self) -> tuple[Path, str, str]:
        """Return independently pinned trust inputs after the owned identity handshake.

        Returns:
            Anchor path, exact anchor digest, and P155 authority SPKI digest.

        Raises:
            EngineUnavailableError: If receipt trust is absent, override-backed, or unverified.
        """
        with self._lock:
            if not self.config.receipt_trust_provisioned:
                raise EngineUnavailableError("AM Engine receipt trust is not provisioned")
            if (
                self.config.runtime_mode is not EngineRuntimeMode.OWNED
                or self.config.binary_path is not None
                or os.environ.get(ENGINE_BINARY_ENV)
                or not self._uses_default_binary_resolver
            ):
                raise EngineUnavailableError("AM Engine receipt trust is unavailable for an arbitrary binary override")
            if self._endpoint is None or self._receipt_trust_identity is None:
                raise EngineUnavailableError(
                    "AM Engine receipt trust is unavailable until the owned /version identity matches the pinned anchor"
                )
            anchor_path = self.config.receipt_trust_anchor_path
            anchor_sha256 = self.config.receipt_anchor_sha256
            authority_pin_sha256 = self.config.receipt_authority_pin_sha256
            assert anchor_path is not None
            assert anchor_sha256 is not None
            assert authority_pin_sha256 is not None
            return anchor_path.resolve(), anchor_sha256, authority_pin_sha256

    def receipt_engine_instance_id(self) -> str:
        """Return the independently checked active engine process identity.

        Raises:
            EngineUnavailableError: If receipt trust or its active process binding is unavailable.
        """
        with self._lock:
            self.receipt_trust_context()
            engine_instance_id = self._receipt_engine_instance_id
            if engine_instance_id is None:
                raise EngineUnavailableError("AM Engine receipt trust has no verified active engine_instance_id")
            return engine_instance_id


def _configured_model_id(model_path: Path) -> str:
    """Resolve a safe registry identifier from the configured GGUF sidecar."""
    resolved = model_path.expanduser().resolve()
    sidecars = (Path(f"{resolved}.meta.json"), resolved.with_suffix(".meta.json"))
    model_id: object = resolved.stem
    for sidecar in sidecars:
        if not sidecar.is_file():
            continue
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EngineUnavailableError("configured AM Engine model sidecar is unreadable", path=str(sidecar)) from exc
        if not isinstance(payload, Mapping):
            raise EngineUnavailableError("configured AM Engine model sidecar must be an object", path=str(sidecar))
        model_id = payload.get("id")
        break
    if (
        not isinstance(model_id, str)
        or not model_id
        or model_id.strip() != model_id
        or model_id in {".", ".."}
        or "/" in model_id
        or "\\" in model_id
        or any(ord(character) < 32 or ord(character) == 127 for character in model_id)
    ):
        raise EngineUnavailableError("configured AM Engine model identifier is invalid", path=str(resolved))
    return model_id


def _reserve_ephemeral_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0))
        return int(listener.getsockname()[1])


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    alive = True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True
    except OSError:
        alive = False
    return alive
