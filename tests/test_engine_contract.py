"""Real-process contract tests for the first-party Rust AM Engine API."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from scripts.engine_contract_environment import prepare_private_directory, verify_engine_contract_path

from vetinari.adapters.am_engine_adapter import (
    ENGINE_ERROR_FAILURE_CLASSES,
    _failure_class_for_exception,
)
from vetinari.analytics.cost_models import CostEntry, require_model_pricing
from vetinari.analytics.cost_storage import CostPersistenceConfig
from vetinari.engine.binary import canonical_binary_path
from vetinari.engine.client_types import EngineErrorCode, EngineResponseError
from vetinari.engine.events import EventIngester
from vetinari.engine.supervisor import (
    EngineConfig,
    EngineState,
    EngineSupervisor,
    _write_private_text,
)
from vetinari.exceptions import EngineBinaryMissingError, EngineVersionMismatchError

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_BINARY = REPO_ROOT / "target" / "debug" / ("amw-engine-server.exe" if os.name == "nt" else "amw-engine-server")
TOKEN = "p15-contract-token"
INFERENCE_TOKEN = "p15-inference-token"
OTHER_INFERENCE_TOKEN = "p15-other-inference-token"
OBSERVABILITY_TOKEN = "p15-observability-token"
ADMIN_TOKEN = "p15-admin-token"
FIXTURE_ENV = "AMW_ENGINE_NATIVE_TEST_MODEL"
FIXTURE_FILENAME = "tinyllama-15M-stories-Q2_K.gguf"
FIXTURE_SHA256 = "f7e39dc9f26f3d39bf59e885349c6eec65880f685322d591f53e6cdb46ceb2e9"
FIXTURE_SIZE = 13_717_344
FIM_FIXTURE_ENV = "AMW_ENGINE_NATIVE_FIM_TEST_MODEL"
FIM_FIXTURE_FILENAME = "qwen2.5-coder-0.5b-instruct-q2_k.gguf"
FIM_FIXTURE_SHA256 = "f9bddf294ef15c80bb64a2cdcf15d5b25caf88fb4f4a12383bc9f7a01a09c2e3"
FIM_FIXTURE_SIZE = 415_182_720


@dataclass(frozen=True, slots=True)
class _RunningEngine:
    port: int
    healthy_model: str
    corrupt_model: str
    corrupt_path: Path
    log_path: Path


def _require_engine_binary(path: Path) -> Path:
    if not path.is_file():
        raise EngineBinaryMissingError("AM Engine binary is missing", path=str(path))
    resolved = path.resolve()
    identity_envs = (
        (
            "AMW_ENGINE_CONTRACT_BINARY",
            "AMW_ENGINE_CONTRACT_BINARY_SHA256",
            "AMW_ENGINE_CONTRACT_BINARY_SIZE",
        ),
        (
            "AMW_ENGINE_BINARY_WITHOUT_CONTRACT_CONTROLS",
            "AMW_ENGINE_BINARY_WITHOUT_CONTRACT_CONTROLS_SHA256",
            "AMW_ENGINE_BINARY_WITHOUT_CONTRACT_CONTROLS_SIZE",
        ),
    )
    for path_env, digest_env, size_env in identity_envs:
        configured = os.environ.get(path_env)
        if configured and Path(configured).resolve() == resolved:
            expected_sha256 = os.environ.get(digest_env)
            expected_size = os.environ.get(size_env)
            if not expected_sha256 or not expected_size:
                raise RuntimeError(f"prepared engine binary is missing its identity record: {path_env}")
            return verify_engine_contract_path(
                resolved,
                expected_sha256=expected_sha256,
                expected_size=int(expected_size),
            )
    return resolved


def _native_fixture() -> Path:
    raw = os.environ.get(FIXTURE_ENV)
    if not raw:
        pytest.fail(
            f"{FIXTURE_ENV} must name the governed CPU GGUF fixture; "
            "the real-engine contract tier never skips when it is absent"
        )
    fixture = Path(raw).expanduser().resolve()
    if not fixture.is_file():
        pytest.fail(f"{FIXTURE_ENV} does not name a regular file: {fixture}")
    assert fixture.name == FIXTURE_FILENAME, "the native contract fixture filename is not governed"
    payload = fixture.read_bytes()
    assert len(payload) == FIXTURE_SIZE, "the native contract fixture size is not governed"
    assert hashlib.sha256(payload).hexdigest() == FIXTURE_SHA256, "the native contract fixture digest is not governed"
    return fixture


def _native_fim_fixture() -> Path:
    raw = os.environ.get(FIM_FIXTURE_ENV)
    if not raw:
        pytest.fail(
            f"{FIM_FIXTURE_ENV} must name the governed FIM-capable GGUF fixture; "
            "successful native infill is a mandatory contract tier"
        )
    fixture = Path(raw).expanduser().resolve()
    if not fixture.is_file():
        pytest.fail(f"{FIM_FIXTURE_ENV} does not name a regular file: {fixture}")
    assert fixture.name == FIM_FIXTURE_FILENAME, "the native FIM fixture filename is not governed"
    assert fixture.stat().st_size == FIM_FIXTURE_SIZE, "the native FIM fixture size is not governed"
    digest = hashlib.sha256()
    with fixture.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    assert digest.hexdigest() == FIM_FIXTURE_SHA256, "the native FIM fixture digest is not governed"
    return fixture


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(
    port: int,
    path: str,
    *,
    method: str | None = None,
    body: Mapping[str, Any] | None = None,
    authenticated: bool = True,
    token: str = TOKEN,
    timeout: float = 10.0,
    request_headers: Mapping[str, str] | None = None,
    inject_control_schema: bool = True,
) -> tuple[int, dict[str, Any]]:
    selected_method = method or ("GET" if body is None else "POST")
    base_path = path.split("?", maxsplit=1)[0]
    request_path = path
    if (
        inject_control_schema
        and selected_method == "GET"
        and base_path.startswith("/admin/")
        and "schema_version=" not in path
    ):
        request_path = f"{path}{'&' if '?' in path else '?'}schema_version=1"
    request_body = None if body is None else dict(body)
    if (
        inject_control_schema
        and selected_method == "POST"
        and request_body is not None
        and (base_path.startswith("/admin/") or base_path == "/v1/cancel")
    ):
        request_body.setdefault("schema_version", 1)
    headers = {"Accept": "application/json"}
    if request_body is not None:
        headers["Content-Type"] = "application/json"
    if authenticated:
        headers["Authorization"] = f"Bearer {token}"
    if request_headers is not None:
        headers.update(request_headers)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            selected_method,
            request_path,
            body=None if request_body is None else json.dumps(request_body).encode(),
            headers=headers,
        )
        response = connection.getresponse()
        raw = response.read()
        decoded = json.loads(raw) if raw else {}
        assert isinstance(decoded, dict), f"{path} returned a non-object JSON payload"
        return response.status, decoded
    finally:
        connection.close()


def _error_code(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    assert isinstance(error, Mapping), f"missing typed error envelope: {payload!r}"
    code = error.get("code")
    assert isinstance(code, str), f"missing typed error code: {payload!r}"
    return code


def _write_model_sidecar(path: Path, model_id: str) -> None:
    sidecar = Path(f"{path}.meta.json")
    sidecar.write_text(
        json.dumps({
            "id": model_id,
            "path": str(path.resolve()),
            "aliases": [],
            "draft_pair": None,
        }),
        encoding="utf-8",
    )


@contextmanager
def _running_engine(
    tmp_path: Path,
    *,
    slot_count: int = 2,
    default_ctx: int = 64,
    include_rejected_model: bool = False,
    require_native_fixture: bool = True,
    native_fixture: Path | None = None,
    ram_gb: float = 0.25,
    enable_test_controls: bool = False,
    expect_test_controls: bool = True,
    binary_path: Path | None = None,
    body_read_timeout_ms: int | None = None,
    before_spawn: Callable[[Path, Path], None] | None = None,
) -> Iterator[_RunningEngine]:
    selected_binary = binary_path or Path(os.environ.get("AMW_ENGINE_CONTRACT_BINARY", ENGINE_BINARY))
    binary = _require_engine_binary(selected_binary)
    model_dir = tmp_path / "models"
    model_dir.mkdir(exist_ok=True)
    healthy_path = model_dir / "healthy.gguf"
    corrupt_path = model_dir / "corrupt.gguf"
    if require_native_fixture:
        fixture = native_fixture or _native_fixture()
        fixture_sha256, fixture_size = (
            (FIM_FIXTURE_SHA256, FIM_FIXTURE_SIZE)
            if fixture.name == FIM_FIXTURE_FILENAME
            else (FIXTURE_SHA256, FIXTURE_SIZE)
        )
        shutil.copy2(fixture, healthy_path)
        shutil.copy2(fixture, corrupt_path)
        verify_engine_contract_path(
            healthy_path,
            expected_sha256=fixture_sha256,
            expected_size=fixture_size,
        )
        verify_engine_contract_path(
            corrupt_path,
            expected_sha256=fixture_sha256,
            expected_size=fixture_size,
        )
        _write_model_sidecar(healthy_path, "healthy")
        _write_model_sidecar(corrupt_path, "corrupt")
    if include_rejected_model:
        (model_dir / "rejected.gguf").write_bytes(b"not a GGUF model")

    port = _free_port()
    token_path = tmp_path / "auth-token"
    _write_private_text(token_path, TOKEN)
    auth_policy_path = tmp_path / "auth-policy.json"
    credentials = (
        ("contract-owner", TOKEN, ["inference", "observability", "admin"]),
        ("inference-a", INFERENCE_TOKEN, ["inference"]),
        ("inference-b", OTHER_INFERENCE_TOKEN, ["inference"]),
        ("observer", OBSERVABILITY_TOKEN, ["observability"]),
        ("administrator", ADMIN_TOKEN, ["admin"]),
    )
    _write_private_text(
        auth_policy_path,
        json.dumps({
            "schema_version": 2,
            "credentials": [
                {
                    "principal_id": principal_id,
                    "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    "token_byte_length": len(token.encode("utf-8")),
                    "scopes": scopes,
                }
                for principal_id, token, scopes in credentials
            ],
        }),
    )
    session_dir = tmp_path / "sessions"
    log_dir = tmp_path / "logs"
    prepare_private_directory(session_dir)
    log_dir.mkdir(exist_ok=True)
    config_path = tmp_path / "engine.toml"
    config_path.write_text(
        f'''[server]
host = "127.0.0.1"
port = {port}
auth_token_path = "{token_path.as_posix()}"
auth_policy_path = "{auth_policy_path.as_posix()}"
[models]
dirs = ["{model_dir.as_posix()}"]
[budgets]
vram_gb = 0.0
ram_gb = {ram_gb}
margin_pct = 10.0
[slots]
count = {slot_count}
default_ctx = {default_ctx}
[kv]
cache_type_k = "q8_0"
cache_type_v = "f16"
session_dir = "{session_dir.as_posix()}"
[idle]
keep_alive = "5m"
[scheduler]
preemption = true
batch_token_budget = 64
[log]
level = "warn"
dir = "{log_dir.as_posix()}"
''',
        encoding="utf-8",
    )
    if before_spawn is not None:
        before_spawn(healthy_path, corrupt_path)
    if require_native_fixture:
        verify_engine_contract_path(
            healthy_path,
            expected_sha256=fixture_sha256,
            expected_size=fixture_size,
        )
        verify_engine_contract_path(
            corrupt_path,
            expected_sha256=fixture_sha256,
            expected_size=fixture_size,
        )
    binary = _require_engine_binary(binary)

    stdout_path = tmp_path / "server.stdout.log"
    stderr_path = tmp_path / "server.stderr.log"
    stdout_stream = stdout_path.open("wb")
    stderr_stream = stderr_path.open("wb")
    process_env = os.environ.copy()
    if enable_test_controls:
        process_env["AMW_ENGINE_ENABLE_TEST_CONTROLS"] = "1"
    if body_read_timeout_ms is not None:
        assert enable_test_controls, "short body deadlines are a contract-test control"
        process_env["AMW_ENGINE_TEST_BODY_READ_TIMEOUT_MS"] = str(body_read_timeout_ms)
    process = subprocess.Popen(
        [str(binary), "--config", str(config_path)],
        cwd=str(REPO_ROOT),
        env=process_env,
        stdout=stdout_stream,
        stderr=stderr_stream,
    )

    def process_diagnostics() -> tuple[bytes, bytes]:
        stdout_stream.flush()
        stderr_stream.flush()
        return stdout_path.read_bytes()[-2000:], stderr_path.read_bytes()[-4000:]

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process_diagnostics()
            stdout_stream.close()
            stderr_stream.close()
            pytest.fail(f"engine exited during startup: stdout={stdout!r} stderr={stderr!r}")
        try:
            status, health = _request(port, "/health", authenticated=False, timeout=1.0)
            if status == 200 and health.get("status") == "ok":
                break
        except OSError:
            time.sleep(0.05)
    else:
        process.kill()
        process.wait(timeout=5)
        stdout, stderr = process_diagnostics()
        stdout_stream.close()
        stderr_stream.close()
        pytest.fail(f"engine did not become healthy: stdout={stdout!r} stderr={stderr!r}")
    try:
        status, version = _request(port, "/version")
        assert status == 200
        if require_native_fixture:
            assert "cpu" in version.get("build_flags", []), (
                "contract test requires amw-engine-server built with --features cpu"
            )
        if enable_test_controls:
            controls_compiled = "contract-test-controls" in version.get("build_flags", [])
            assert controls_compiled is expect_test_controls
        yield _RunningEngine(port, "healthy", "corrupt", corrupt_path, log_dir / "engine.jsonl")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stdout_stream.close()
        stderr_stream.close()


def _load_model(engine: _RunningEngine, model_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
    status, payload = _request(
        engine.port,
        "/admin/models/load",
        body={"model_id": model_id},
        timeout=timeout,
    )
    assert status == 200, payload
    assert payload["loaded"] == model_id
    return payload


def _forced_text_token(engine: _RunningEngine, model_id: str | None = None) -> int:
    selected_model = model_id or engine.healthy_model
    status, payload = _request(
        engine.port,
        "/v1/tokenize",
        body={"model": selected_model, "items": ["x"], "add_special": False},
    )
    assert status == 200, payload
    tokens = payload["results"][0]
    assert tokens, "the governed model must tokenize a non-empty text fragment"
    return int(tokens[-1])


def _wait_for_busy(engine: _RunningEngine, expected: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_slots: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, last_slots = _request(engine.port, "/admin/slots", timeout=1.0)
        assert status == 200, last_slots
        if sum(slot["busy"] for slot in last_slots["slots"]) == expected:
            return
        time.sleep(0.005)
    pytest.fail(f"engine did not reach busy={expected}: {last_slots!r}")


def _read_ndjson_until(
    engine: _RunningEngine,
    predicate: Any,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=timeout)
    connection.request(
        "GET",
        "/events",
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/x-ndjson"},
    )
    response = connection.getresponse()
    assert response.status == 200
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            raw = response.readline()
            if not raw:
                break
            event = json.loads(raw)
            if predicate(event):
                return event
    finally:
        connection.close()
    pytest.fail("live engine event stream did not produce the expected event")


def _send_completion_stream(
    engine: _RunningEngine,
    *,
    max_tokens: int = 24,
    model_id: str | None = None,
    priority: str = "interactive",
    forced_token: int | None = None,
    prompt: str = "Continue this sentence:",
    token: str = TOKEN,
    stop: list[str] | None = None,
    request_id: str | None = None,
) -> http.client.HTTPConnection:
    selected_model = model_id or engine.healthy_model
    selected_token = forced_token if forced_token is not None else _forced_text_token(engine, selected_model)
    connection = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=20.0)
    connection.request(
        "POST",
        "/v1/completions",
        body=json.dumps({
            "model": selected_model,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "seed": 7,
            "logit_bias": {str(selected_token): 100.0},
            "priority_class": priority,
            "stream": True,
            "stop": stop or [],
        }).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **({"x-request-id": request_id} if request_id is not None else {}),
        },
    )
    return connection


def _open_completion_stream(
    engine: _RunningEngine,
    *,
    max_tokens: int = 24,
    model_id: str | None = None,
    priority: str = "interactive",
    forced_token: int | None = None,
    prompt: str = "Continue this sentence:",
    token: str = TOKEN,
    stop: list[str] | None = None,
    request_id: str | None = None,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    connection = _send_completion_stream(
        engine,
        max_tokens=max_tokens,
        model_id=model_id,
        priority=priority,
        forced_token=forced_token,
        prompt=prompt,
        token=token,
        stop=stop,
        request_id=request_id,
    )
    response = connection.getresponse()
    assert response.status == 200
    return connection, response


def _prompt_near_token_count(engine: _RunningEngine, target: int) -> tuple[str, int]:
    low, high = 1, target * 3
    best = ("x", 0)
    while low <= high:
        repeats = (low + high) // 2
        prompt = "x " * repeats
        status, payload = _request(
            engine.port,
            "/v1/tokenize",
            body={
                "model": engine.healthy_model,
                "items": [prompt],
                "add_special": True,
            },
        )
        assert status == 200, payload
        token_count = len(payload["results"][0])
        if token_count <= target:
            best = (prompt, token_count)
            low = repeats + 1
        else:
            high = repeats - 1
    assert best[1] >= target - 4, best[1]
    return best


def _next_sse_data(response: http.client.HTTPResponse) -> str:
    while True:
        raw = response.readline()
        assert raw, "SSE response disconnected before [DONE]"
        line = raw.decode("utf-8").strip()
        if line.startswith("data:"):
            return line.removeprefix("data:").strip()


def _collect_sse(response: http.client.HTTPResponse, first: str | None = None) -> list[Any]:
    events: list[Any] = []
    data = first
    while True:
        data = data if data is not None else _next_sse_data(response)
        if data == "[DONE]":
            events.append(data)
            return events
        events.append(json.loads(data))
        data = None


def _cost_config(tmp_path: Path) -> CostPersistenceConfig:
    return CostPersistenceConfig(
        entries_path=tmp_path / "costs.jsonl",
        budget_alerts_path=tmp_path / "alerts.jsonl",
        max_bytes=1024 * 1024,
        backup_count=1,
        budget_limit_usd=100.0,
    )


def test_missing_binary_and_version_mismatch_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(EngineBinaryMissingError):
        _require_engine_binary(tmp_path / "absent-engine")

    # When the hermetic boundary provisions an isolated binary, make the legacy
    # fallback unusable so this proof cannot silently consume a stale
    # target/debug binary from a developer checkout. Direct developer runs still
    # retain their explicit workspace-binary fallback.
    if os.environ.get("AMW_ENGINE_BINARY_WITHOUT_CONTRACT_CONTROLS"):
        monkeypatch.setattr(sys.modules[__name__], "ENGINE_BINARY", tmp_path / "workspace-target-not-built")
    binary = _require_engine_binary(Path(os.environ.get("AMW_ENGINE_BINARY_WITHOUT_CONTRACT_CONTROLS", ENGINE_BINARY)))
    assert canonical_binary_path(tmp_path).name == binary.name

    supervisor = EngineSupervisor(
        EngineConfig(binary_path=binary, expected_version="0.0.0-contract-mismatch"),
        runtime_dir=tmp_path / "runtime",
        process_factory=lambda *_args, **_kwargs: pytest.fail("version-mismatched binary was spawned"),
        event_ingester_factory=lambda: EventIngester(persistence=_cost_config(tmp_path)),
    )
    with pytest.raises(EngineVersionMismatchError, match=r"0\.0\.0-contract-mismatch"):
        supervisor.ensure_running()
    assert supervisor.state is EngineState.VERSION_MISMATCH


def test_model_substitution_between_preparation_and_spawn_fails_closed(tmp_path: Path) -> None:
    def substitute(healthy_path: Path, _corrupt_path: Path) -> None:
        healthy_path.write_bytes(b"substituted after preparation")

    with pytest.raises(RuntimeError, match="identity changed after preparation"):
        with _running_engine(tmp_path, before_spawn=substitute):
            pytest.fail("substituted model reached the engine process")


def test_error_vocabulary_is_a_bijection_into_adapter_failure_classes() -> None:
    expected = {code.value for code in EngineErrorCode}
    assert set(ENGINE_ERROR_FAILURE_CLASSES) == expected
    assert set(ENGINE_ERROR_FAILURE_CLASSES.values()) == expected
    assert len(set(ENGINE_ERROR_FAILURE_CLASSES.values())) == len(expected)
    for code in EngineErrorCode:
        error = EngineResponseError(code.value, code=code, retryable=False)
        failure_class = _failure_class_for_exception(error)
        assert failure_class == code.value
        assert EngineErrorCode(failure_class) is code


def test_every_non_health_route_requires_bearer_authentication(tmp_path: Path) -> None:
    routes = (
        ("GET", "/readyz"),
        ("GET", "/metrics"),
        ("GET", "/version"),
        ("GET", "/events"),
        ("POST", "/v1/completions"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/infill"),
        ("POST", "/v1/embeddings"),
        ("GET", "/v1/models"),
        ("POST", "/v1/tokenize"),
        ("POST", "/v1/count"),
        ("POST", "/v1/cancel"),
        ("POST", "/admin/models/load"),
        ("POST", "/admin/models/unload"),
        ("GET", "/admin/models/status"),
        ("GET", "/admin/models/catalog"),
        ("POST", "/admin/lora/register"),
        ("POST", "/admin/lora/swap"),
        ("GET", "/admin/slots"),
        ("POST", "/admin/drain"),
        ("POST", "/admin/config/reload"),
        ("POST", "/admin/prefix"),
        ("POST", "/admin/sessions"),
    )
    with _running_engine(tmp_path, require_native_fixture=False) as engine:
        status, health = _request(engine.port, "/health", authenticated=False)
        assert status == 200
        assert health["status"] == "ok"
        assert health["schema_version"] == 1
        for attempt, (method, path) in enumerate(routes, start=1):
            status, payload = _request(
                engine.port,
                path,
                method=method,
                body={} if method == "POST" else None,
                authenticated=False,
            )
            assert status == (401 if attempt < 5 else 429), path
            assert _error_code(payload) == EngineErrorCode.UNAUTHORIZED.value, path

        status, payload = _request(
            engine.port,
            "/v1/completions",
            method="GET",
            authenticated=False,
        )
        assert status == 429
        assert _error_code(payload) == EngineErrorCode.UNAUTHORIZED.value

        status, payload = _request(
            engine.port,
            "/version",
            authenticated=True,
            token="wrong-token",
        )
        assert status == 401
        assert _error_code(payload) == EngineErrorCode.UNAUTHORIZED.value

        status, payload = _request(engine.port, "/version")
        assert status == 200, payload


def test_invalid_candidate_throttles_without_locking_out_valid_bearer(tmp_path: Path) -> None:
    def auth_probe(port: int, token: str) -> tuple[int, dict[str, Any], str | None]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10.0)
        try:
            connection.request(
                "GET",
                "/version",
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            )
            response = connection.getresponse()
            raw = response.read()
            payload = json.loads(raw) if raw else {}
            assert isinstance(payload, dict)
            return response.status, payload, response.getheader("Retry-After")
        finally:
            connection.close()

    with _running_engine(tmp_path, require_native_fixture=False) as engine:
        for _attempt in range(4):
            status, payload, retry_after = auth_probe(engine.port, "wrong-token")
            assert status == 401
            assert retry_after is None
            assert _error_code(payload) == EngineErrorCode.UNAUTHORIZED.value

        status, payload, retry_after = auth_probe(engine.port, "wrong-token")
        assert status == 429
        assert retry_after == "30"
        assert _error_code(payload) == EngineErrorCode.UNAUTHORIZED.value
        assert payload["error"]["retryable"] is True

        status, payload, retry_after = auth_probe(engine.port, TOKEN)
        assert status == 200, payload
        assert retry_after is None

        status, payload, retry_after = auth_probe(engine.port, "wrong-token")
        assert status == 429
        assert retry_after == "30"
        assert _error_code(payload) == EngineErrorCode.UNAUTHORIZED.value

        status, metrics_payload = _request(engine.port, "/metrics")
        assert status == 200, metrics_payload
        metrics = metrics_payload["metrics"]
        assert metrics["authentication_failures"] == 6
        assert metrics["authentication_throttled_requests"] == 2
        assert "authentication_recoveries" not in metrics
        assert metrics["authentication_source_evictions"] == 0
        assert metrics["authentication_tracked_sources"] == 1


def test_model_catalog_is_bounded_stable_and_path_redacted(tmp_path: Path) -> None:
    with _running_engine(tmp_path, include_rejected_model=True) as engine:
        status, first = _request(
            engine.port,
            "/admin/models/catalog?limit=1&rejected_limit=1",
        )
        assert status == 200, first
        assert first["model_count"] == 2
        assert first["rejected_count"] == 1
        assert [model["id"] for model in first["models"]] == ["corrupt"]
        assert first["next_model_offset"] == 1
        assert first["next_rejected_offset"] is None
        assert first["rejected"] == [
            {
                "candidate_name": "rejected.gguf",
                "reason_code": "integrity",
                "reason": "GGUF metadata or tensor bounds failed validation",
            }
        ]
        serialized = json.dumps(first)
        assert str(tmp_path.resolve()) not in serialized.replace("\\\\", "\\")
        assert "not a GGUF model" not in serialized

        status, second = _request(
            engine.port,
            "/admin/models/catalog?offset=1&limit=1&rejected_offset=1&rejected_limit=1",
        )
        assert status == 200, second
        assert [model["id"] for model in second["models"]] == ["healthy"]
        assert second["rejected"] == []
        assert second["next_model_offset"] is None

        status, unbounded = _request(engine.port, "/admin/models/catalog?limit=257")
        assert status == 422
        assert _error_code(unbounded) == EngineErrorCode.UNSUPPORTED_PARAM.value


def test_route_scopes_are_enforced_exactly_once_per_credential(tmp_path: Path) -> None:
    with _running_engine(tmp_path, require_native_fixture=False) as engine:
        permitted = (
            (OBSERVABILITY_TOKEN, "/version"),
            (INFERENCE_TOKEN, "/v1/models"),
            (ADMIN_TOKEN, "/admin/slots"),
            (ADMIN_TOKEN, "/admin/models/catalog?schema_version=1"),
        )
        for token, path in permitted:
            status, payload = _request(engine.port, path, token=token)
            assert status == 200, (path, payload)

        forbidden = (
            (OBSERVABILITY_TOKEN, "/v1/models"),
            (OBSERVABILITY_TOKEN, "/admin/slots"),
            (OBSERVABILITY_TOKEN, "/admin/models/catalog?schema_version=1"),
            (INFERENCE_TOKEN, "/version"),
            (INFERENCE_TOKEN, "/admin/slots"),
            (INFERENCE_TOKEN, "/admin/models/catalog?schema_version=1"),
            (ADMIN_TOKEN, "/version"),
            (ADMIN_TOKEN, "/v1/models"),
        )
        for token, path in forbidden:
            status, payload = _request(engine.port, path, token=token)
            assert status == 403, (path, payload)
            assert _error_code(payload) == EngineErrorCode.UNAUTHORIZED.value


def test_real_completion_exact_tokens_live_events_and_cost_reconciliation(tmp_path: Path) -> None:
    prompt = "Hello from Vetinari"
    with _running_engine(tmp_path) as engine:
        loaded = _load_model(engine, engine.healthy_model)
        assert loaded["model"]["context_length"] > 0
        forced_token = _forced_text_token(engine)

        status, count = _request(
            engine.port,
            "/v1/count",
            body={"model": engine.healthy_model, "items": [prompt], "add_special": True},
        )
        assert status == 200
        assert len(count["counts"]) == 1
        exact_prompt_tokens = count["counts"][0]
        assert isinstance(exact_prompt_tokens, int) and exact_prompt_tokens > 0

        status, completion = _request(
            engine.port,
            "/v1/completions",
            body={
                "model": engine.healthy_model,
                "prompt": prompt,
                "max_tokens": 4,
                "temperature": 0.0,
                "seed": 7,
                "logit_bias": {str(forced_token): 100.0},
            },
            timeout=30.0,
            request_headers={
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                "x-request-id": "contract-request-id",
            },
        )
        assert status == 200, completion
        assert completion["text"], "native completion must not be a canned empty response"
        assert completion["prompt_tokens"] == exact_prompt_tokens
        assert completion["completion_tokens"] == 4
        assert completion["request_id"] == completion["id"] == "contract-request-id"
        assert completion["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"

        event = _read_ndjson_until(
            engine,
            lambda row: row.get("event") == "request_complete" and row.get("request_id") == completion["request_id"],
        )
        assert event["input_tokens"] == completion["prompt_tokens"]
        assert event["output_tokens"] == completion["completion_tokens"]
        assert event["trace_id"] == completion["trace_id"]
        proposed = event["speculation_proposed_tokens"]
        accepted = event["speculation_accepted_tokens"]
        assert isinstance(proposed, int) and proposed >= 0
        assert isinstance(accepted, int) and 0 <= accepted <= proposed
        if proposed == 0:
            assert "spec_accept_rate" not in event
        else:
            assert event["spec_accept_rate"] == accepted / proposed

        persisted: list[CostEntry] = []
        ingester = EventIngester(
            batch_size=1,
            persistence=_cost_config(tmp_path),
            sla_tracker=type(
                "SLA",
                (),
                {
                    "record_latency": lambda *_args, **_kwargs: None,
                    "record_request": lambda *_args, **_kwargs: None,
                },
            )(),
            span_factory=lambda *_args, **_kwargs: nullcontext(),
            persist_batch=lambda entries, _config: persisted.extend(entries),
        )
        assert ingester.submit_nowait(event)
        asyncio.run(ingester.drain())
        assert len(persisted) == 1
        entry = persisted[0]
        assert (entry.input_tokens, entry.output_tokens) == (
            completion["prompt_tokens"],
            completion["completion_tokens"],
        )
        expected_cost = require_model_pricing("am_engine:*").compute(
            entry.input_tokens,
            entry.output_tokens,
        )
        assert entry.cost_usd == expected_cost


def test_workload_role_metrics_use_only_canonical_bounded_buckets(tmp_path: Path) -> None:
    with _running_engine(tmp_path) as engine:
        _load_model(engine, engine.healthy_model)
        forced_token = _forced_text_token(engine)
        for role in ("foreman", "worker", "inspector", "attacker-unique-role"):
            status, completion = _request(
                engine.port,
                "/v1/completions",
                body={
                    "model": engine.healthy_model,
                    "prompt": f"role {role}",
                    "max_tokens": 1,
                    "temperature": 0.0,
                    "logit_bias": {str(forced_token): 100.0},
                    "role": role,
                },
                timeout=30.0,
            )
            assert status == 200, completion

        status, payload = _request(engine.port, "/metrics")
        assert status == 200, payload
        per_role = payload["metrics"]["per_role"]
        assert set(per_role) == {"foreman", "worker", "inspector", "unknown"}
        for aggregate in per_role.values():
            assert aggregate["requests"] == 1
            assert aggregate["speculation_proposed_tokens"] >= 0
            assert 0 <= aggregate["speculation_accepted_tokens"] <= aggregate["speculation_proposed_tokens"]


def test_generation_and_admin_route_families_have_live_success_and_typed_rejection(
    tmp_path: Path,
) -> None:
    with _running_engine(tmp_path) as engine:
        loaded = _load_model(engine, engine.healthy_model)["model"]
        forced_token = _forced_text_token(engine)
        bias = {str(forced_token): 100.0}

        status, ready = _request(engine.port, "/readyz")
        assert status == 200 and ready["ready"] is True
        status, metrics = _request(engine.port, "/metrics")
        assert status == 200 and isinstance(metrics["metrics"], dict)
        status, models = _request(engine.port, "/v1/models")
        assert status == 200
        assert any(model["id"] == engine.healthy_model for model in models["data"])

        status, chat = _request(
            engine.port,
            "/v1/chat/completions",
            body={
                "model": engine.healthy_model,
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 2,
                "temperature": 0.0,
                "logit_bias": bias,
            },
            timeout=30.0,
        )
        assert status == 200, chat
        assert chat["content"] and chat["usage"]["completion_tokens"] > 0

        status, tokenized = _request(
            engine.port,
            "/v1/tokenize",
            body={"model": engine.healthy_model, "items": ["route matrix"], "add_special": True},
        )
        assert status == 200 and tokenized["results"][0]
        status, counted = _request(
            engine.port,
            "/v1/count",
            body={"model": engine.healthy_model, "items": ["route matrix"], "add_special": True},
        )
        assert status == 200
        assert counted["counts"] == [len(tokenized["results"][0])]

        status, embedding = _request(
            engine.port,
            "/v1/embeddings",
            body={"model": engine.healthy_model, "input": ["embed this"]},
            timeout=30.0,
        )
        assert loaded["supports_embeddings"] is True
        assert loaded["embedding_length"] > 0
        assert status == 200, embedding
        assert set(embedding) == {"schema_version", "object", "data"}
        assert len(embedding["data"][0]["embedding"]) == loaded["embedding_length"]
        assert all(math.isfinite(value) for value in embedding["data"][0]["embedding"])

        status, infill = _request(
            engine.port,
            "/v1/infill",
            body={
                "model": engine.healthy_model,
                "prompt": "def answer():\n    ",
                "suffix": "\n",
                "max_tokens": 2,
                "temperature": 0.0,
                "logit_bias": bias,
            },
            timeout=30.0,
        )
        assert loaded["supports_fim"] is False
        assert status == 422
        assert _error_code(infill) == EngineErrorCode.UNSUPPORTED_PARAM.value

        status, model_status = _request(
            engine.port,
            f"/admin/models/status?model_id={engine.healthy_model}",
        )
        assert status == 200 and model_status["models"][0]["id"] == engine.healthy_model
        status, slots = _request(engine.port, "/admin/slots")
        assert status == 200
        assert slots["slots"][0]["model_id"] == engine.healthy_model
        status, reloaded = _request(engine.port, "/admin/config/reload", body={})
        assert status == 200 and reloaded["reloaded"] == []

        content = "stable system prefix"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        status, registered = _request(
            engine.port,
            "/admin/prefix",
            body={
                "action": "register",
                "name": "contract-prefix",
                "content": content,
                "content_hash": content_hash,
                "model": engine.healthy_model,
            },
        )
        assert status == 200, registered
        assert registered["content_hash"] == content_hash
        assert registered["token_count"] > 0
        for action, expected_pin in (("pin", True), ("unpin", False)):
            status, prefix = _request(
                engine.port,
                "/admin/prefix",
                body={
                    "action": action,
                    "name": "contract-prefix",
                    "content_hash": content_hash,
                    "model": engine.healthy_model,
                },
            )
            assert status == 200, prefix
            assert prefix["pinned"] is expected_pin

        for action in ("create", "save", "resume"):
            status, session = _request(
                engine.port,
                "/admin/sessions",
                body={
                    "action": action,
                    "session_id": "contract-session",
                    "model": engine.healthy_model,
                },
            )
            assert status == 200, session
            assert session["action"] == action

        status, lora_registration = _request(
            engine.port,
            "/admin/lora/register",
            body={
                "id": "contract-adapter",
                "root_id": "models-0",
                "relative_path": "corrupt.gguf",
                "size_bytes": FIXTURE_SIZE,
                "sha256": FIXTURE_SHA256,
                "base_model_sha256": FIXTURE_SHA256,
                "scale": 1.0,
            },
            timeout=30.0,
        )
        assert status == 200, lora_registration
        assert lora_registration == {
            "schema_version": 1,
            "adapter_id": "contract-adapter",
            "sha256": FIXTURE_SHA256,
        }
        status, lora = _request(
            engine.port,
            "/admin/lora/swap",
            body={"model_id": engine.healthy_model, "adapter_id": None},
        )
        assert status == 200 and lora["swapped"] is True
        status, drain = _request(engine.port, "/admin/drain", body={"enabled": False})
        assert status == 200 and drain["draining"] is False
        status, missing_schema = _request(
            engine.port,
            "/admin/drain",
            body={"enabled": False},
            inject_control_schema=False,
        )
        assert status == 422
        assert _error_code(missing_schema) == EngineErrorCode.UNSUPPORTED_PARAM.value
        status, wrong_method = _request(engine.port, "/v1/completions", method="GET")
        assert status == 405
        assert _error_code(wrong_method) == EngineErrorCode.UNSUPPORTED_PARAM.value
        status, malformed_query = _request(
            engine.port,
            "/events?generation=current&after_cursor=not-an-integer",
            method="GET",
        )
        assert status == 422
        assert _error_code(malformed_query) == EngineErrorCode.UNSUPPORTED_PARAM.value

        malformed_cases = (
            ("/v1/chat/completions", {"schema_version": 999, "messages": []}, "version_mismatch"),
            ("/v1/infill", {"model": engine.healthy_model, "prompt": "x"}, "unsupported_param"),
            ("/v1/embeddings", {"model": engine.healthy_model, "input": []}, "unsupported_param"),
            ("/v1/tokenize", {"model": engine.healthy_model, "items": []}, "unsupported_param"),
            ("/v1/count", {"model": engine.healthy_model, "items": []}, "unsupported_param"),
            ("/admin/models/load", {"model_id": ""}, "unsupported_param"),
            ("/admin/models/unload", {}, "unsupported_param"),
            (
                "/admin/lora/register",
                {
                    "id": "unsafe-adapter",
                    "root_id": "models-0",
                    "relative_path": str(engine.corrupt_path.resolve()),
                    "size_bytes": 1,
                    "sha256": "a" * 64,
                    "base_model_sha256": "b" * 64,
                    "scale": 1.0,
                },
                "unsupported_param",
            ),
            ("/admin/lora/swap", {"model_id": "absent", "adapter_id": None}, "model_not_loaded"),
            ("/admin/drain", {"enabled": False, "unexpected": True}, "unsupported_param"),
            ("/admin/config/reload", {"log_level": "verbose"}, "unsupported_param"),
            (
                "/admin/prefix",
                {"action": "erase", "name": "x", "content_hash": "x"},
                "unsupported_param",
            ),
            (
                "/admin/sessions",
                {"action": "erase", "session_id": "x", "model": engine.healthy_model},
                "unsupported_param",
            ),
        )
        for path, body, expected_code in malformed_cases:
            status, rejection = _request(engine.port, path, body=body)
            assert status >= 400, path
            assert _error_code(rejection) == expected_code, path

        status, absent = _request(engine.port, "/admin/models/status?model_id=absent")
        assert status == 404
        assert _error_code(absent) == EngineErrorCode.MODEL_NOT_LOADED.value
        status, unloaded = _request(
            engine.port,
            "/admin/models/unload",
            body={"model_id": engine.healthy_model},
        )
        assert status == 200 and unloaded["unloaded"] == engine.healthy_model
        status, absent = _request(
            engine.port,
            "/admin/models/unload",
            body={"model_id": engine.healthy_model},
        )
        assert status == 404
        assert _error_code(absent) == EngineErrorCode.MODEL_NOT_LOADED.value


def test_session_restores_durable_kv_after_engine_restart(tmp_path: Path) -> None:
    """A saved session must remain usable after the owning process is replaced."""
    session_id = "durable-contract-session"
    with _running_engine(tmp_path) as engine:
        _load_model(engine, engine.healthy_model)
        forced_token = _forced_text_token(engine)
        status, created = _request(
            engine.port,
            "/admin/sessions",
            body={
                "action": "create",
                "session_id": session_id,
                "model": engine.healthy_model,
            },
        )
        assert status == 200, created
        status, generated = _request(
            engine.port,
            "/v1/completions",
            body={
                "model": engine.healthy_model,
                "prompt": "Persist this conversation state:",
                "max_tokens": 2,
                "temperature": 0.0,
                "logit_bias": {str(forced_token): 100.0},
                "session_id": session_id,
            },
        )
        assert status == 200, generated
        assert generated.get("completion_tokens") == 2, generated
        status, saved = _request(
            engine.port,
            "/admin/sessions",
            body={
                "action": "save",
                "session_id": session_id,
                "model": engine.healthy_model,
            },
        )
        assert status == 200, saved

    with _running_engine(tmp_path) as restarted:
        _load_model(restarted, restarted.healthy_model)
        forced_token = _forced_text_token(restarted)
        status, resumed = _request(
            restarted.port,
            "/admin/sessions",
            body={
                "action": "resume",
                "session_id": session_id,
                "model": restarted.healthy_model,
            },
        )
        assert status == 200, resumed
        status, continued = _request(
            restarted.port,
            "/v1/completions",
            body={
                "model": restarted.healthy_model,
                "prompt": "Continue after restart:",
                "max_tokens": 1,
                "temperature": 0.0,
                "logit_bias": {str(forced_token): 100.0},
                "session_id": session_id,
            },
        )
        assert status == 200, continued
        assert continued.get("completion_tokens") == 1, continued
        assert continued["text"]


def test_empty_session_create_save_survives_process_restart(tmp_path: Path) -> None:
    """Create and save must durably distinguish an empty session after restart."""
    session_id = "empty-restart-session"
    with _running_engine(tmp_path) as engine:
        _load_model(engine, engine.healthy_model)
        for action in ("create", "save"):
            status, payload = _request(
                engine.port,
                "/admin/sessions",
                body={
                    "action": action,
                    "session_id": session_id,
                    "model": engine.healthy_model,
                },
            )
            assert status == 200, payload

    with _running_engine(tmp_path) as restarted:
        _load_model(restarted, restarted.healthy_model)
        status, resumed = _request(
            restarted.port,
            "/admin/sessions",
            body={
                "action": "resume",
                "session_id": session_id,
                "model": restarted.healthy_model,
            },
        )
        assert status == 200, resumed
        status, generated = _request(
            restarted.port,
            "/v1/completions",
            body={
                "model": restarted.healthy_model,
                "prompt": "fresh empty session",
                "max_tokens": 1,
                "session_id": session_id,
            },
        )
        assert status == 200, generated
        assert generated.get("completion_tokens") == 1, generated


def test_corrupt_model_grammar_failure_context_overflow_and_sibling_survival(tmp_path: Path) -> None:
    with _running_engine(tmp_path) as engine:
        _load_model(engine, engine.healthy_model)

        engine.corrupt_path.write_bytes(b"GGUF")
        status, corrupt = _request(
            engine.port,
            "/admin/models/load",
            body={"model_id": engine.corrupt_model},
            timeout=30.0,
        )
        assert status == 422
        assert _error_code(corrupt) == EngineErrorCode.MODEL_CORRUPT.value

        bad_request = {
            "model": engine.healthy_model,
            "prompt": "bad grammar request",
            "max_tokens": 4,
            "grammar": 'root ::= ( "unterminated"',
        }
        good_request = {
            "model": engine.healthy_model,
            "prompt": "healthy sibling request",
            "max_tokens": 4,
            "temperature": 0.0,
            "logit_bias": {str(_forced_text_token(engine)): 100.0},
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            bad_future = pool.submit(
                _request,
                engine.port,
                "/v1/completions",
                body=bad_request,
                timeout=30.0,
            )
            good_future = pool.submit(
                _request,
                engine.port,
                "/v1/completions",
                body=good_request,
                timeout=30.0,
            )
            bad_status, bad = bad_future.result()
            good_status, good = good_future.result()
        assert bad_status == 422
        assert _error_code(bad) == EngineErrorCode.GRAMMAR_INVALID.value
        assert good_status == 200, good
        assert good["completion_tokens"] > 0

        context_limit = int(_load_model(engine, engine.healthy_model)["model"]["context_length"])
        oversized_prompt = "token "
        while True:
            status, count = _request(
                engine.port,
                "/v1/count",
                body={
                    "model": engine.healthy_model,
                    "items": [oversized_prompt],
                    "add_special": True,
                },
            )
            assert status == 200
            if count["counts"][0] + 1 > context_limit:
                break
            oversized_prompt *= 2
        status, overflow = _request(
            engine.port,
            "/v1/completions",
            body={
                "model": engine.healthy_model,
                "prompt": oversized_prompt,
                "max_tokens": 1,
            },
            timeout=30.0,
        )
        assert status == 413, overflow
        assert _error_code(overflow) == EngineErrorCode.CONTEXT_OVERFLOW.value


def test_shared_batch_priority_and_exact_token_budgets(tmp_path: Path) -> None:
    with _running_engine(tmp_path, default_ctx=256) as engine:
        load_started = time.monotonic()
        _load_model(engine, engine.healthy_model)
        assert time.monotonic() - load_started < 10.0, "zero-VRAM CPU model load stalled"

        forced_token = _forced_text_token(engine)
        status, one_token = _request(
            engine.port,
            "/v1/completions",
            body={
                "model": engine.healthy_model,
                "prompt": "one token",
                "max_tokens": 1,
                "temperature": 0.0,
                "logit_bias": {str(forced_token): 100.0},
            },
        )
        assert status == 200, one_token
        assert one_token["completion_tokens"] == 1
        long_prompt, prompt_tokens = _prompt_near_token_count(engine, 220)
        background_tokens = 256 - prompt_tokens
        assert 32 <= background_tokens <= 40

        background_connection = _send_completion_stream(
            engine,
            max_tokens=background_tokens,
            priority="background",
            forced_token=forced_token,
            prompt=long_prompt,
        )
        _wait_for_busy(engine, 1)
        blocker_connection = _send_completion_stream(
            engine,
            max_tokens=32,
            priority="interactive_blocking",
            forced_token=forced_token,
        )

        def collect_timed(connection: http.client.HTTPConnection) -> tuple[float, list[Any]]:
            response = connection.getresponse()
            assert response.status == 200
            events = _collect_sse(response)
            return time.monotonic(), events

        with ThreadPoolExecutor(max_workers=2) as pool:
            collected = [
                pool.submit(collect_timed, connection) for connection in (background_connection, blocker_connection)
            ]
            timed_events = [future.result() for future in collected]
        for connection in (background_connection, blocker_connection):
            connection.close()
        for (_, events), expected_tokens in zip(
            timed_events,
            (background_tokens, 32),
            strict=True,
        ):
            terminal = next(event for event in events if isinstance(event, dict) and event.get("type") == "finished")
            assert terminal["usage"]["completion_tokens"] == expected_tokens
            assert sum(isinstance(event, dict) and event.get("type") == "delta" for event in events) == expected_tokens
            assert events[-1] == "[DONE]"
        assert timed_events[1][0] <= timed_events[0][0], (
            "interactive-blocking request did not finish before overlapping background work"
        )
        status, slots = _request(engine.port, "/admin/slots")
        assert status == 200
        assert slots["slots"][0]["max_batch_sequences"] >= 2


def test_single_global_slot_transfers_to_blocker_resumes_and_does_not_hoard(
    tmp_path: Path,
) -> None:
    with _running_engine(tmp_path, slot_count=1, default_ctx=256) as engine:
        _load_model(engine, engine.healthy_model)
        _load_model(engine, engine.corrupt_model)
        forced_token = _forced_text_token(engine)
        long_prompt, prompt_tokens = _prompt_near_token_count(engine, 220)
        background_tokens = 256 - prompt_tokens
        assert 32 <= background_tokens <= 40

        background_connection = _send_completion_stream(
            engine,
            max_tokens=background_tokens,
            priority="background",
            forced_token=forced_token,
            prompt=long_prompt,
        )
        _wait_for_busy(engine, 1)

        def collect_at(
            connection: http.client.HTTPConnection,
        ) -> tuple[float, list[Any]]:
            response = connection.getresponse()
            assert response.status == 200
            events = _collect_sse(response)
            return time.monotonic(), events

        blocking_connection = _send_completion_stream(
            engine,
            max_tokens=32,
            priority="interactive_blocking",
            forced_token=forced_token,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            background_future = pool.submit(collect_at, background_connection)
            blocking_future = pool.submit(collect_at, blocking_connection)
            blocking_finished_at, blocking_events = blocking_future.result(timeout=20)
            background_finished_at, background_events = background_future.result(timeout=20)

        background_connection.close()
        blocking_connection.close()
        for events, expected_tokens in (
            (background_events, background_tokens),
            (blocking_events, 32),
        ):
            terminal = next(event for event in events if isinstance(event, dict) and event.get("type") == "finished")
            assert terminal["usage"]["completion_tokens"] == expected_tokens
            assert sum(isinstance(event, dict) and event.get("type") == "delta" for event in events) == expected_tokens
            assert events[-1] == "[DONE]"
        assert blocking_finished_at <= background_finished_at, (
            "the one-slot blocker did not preempt and finish before Background resumed"
        )

        status, slots = _request(engine.port, "/admin/slots")
        assert status == 200
        assert sum(slot["busy"] for slot in slots["slots"]) == 0
        healthy_slots = next(slot for slot in slots["slots"] if slot["model_id"] == engine.healthy_model)
        assert healthy_slots["background_evicted"] >= 1

        status, sibling_model = _request(
            engine.port,
            "/v1/completions",
            body={
                "model": engine.corrupt_model,
                "prompt": "permit release proof",
                "max_tokens": 1,
                "temperature": 0.0,
            },
            timeout=20.0,
        )
        assert status == 200, sibling_model
        assert sibling_model["completion_tokens"] == 1


def test_global_slot_cap_applies_across_loaded_models(tmp_path: Path) -> None:
    with _running_engine(tmp_path) as engine:
        _load_model(engine, engine.healthy_model)
        _load_model(engine, engine.corrupt_model)
        model_ids = [engine.healthy_model, engine.corrupt_model, engine.healthy_model]
        with ThreadPoolExecutor(max_workers=3) as pool:
            opened = [
                pool.submit(_open_completion_stream, engine, max_tokens=8, model_id=model_id) for model_id in model_ids
            ]
            capped_streams = [future.result() for future in opened]
        status, slots = _request(engine.port, "/admin/slots")
        assert status == 200
        assert sum(slot["busy"] for slot in slots["slots"]) <= 2
        with ThreadPoolExecutor(max_workers=3) as pool:
            collected = [pool.submit(_collect_sse, response) for _, response in capped_streams]
            capped_events = [future.result() for future in collected]
        for connection, _ in capped_streams:
            connection.close()
        assert all(events[-1] == "[DONE]" for events in capped_events)


def test_command_flood_does_not_starve_background_generation(tmp_path: Path) -> None:
    with _running_engine(tmp_path) as engine:
        _load_model(engine, engine.healthy_model)
        flood_connection, flood_response = _open_completion_stream(
            engine,
            max_tokens=24,
            priority="background",
        )
        with ThreadPoolExecutor(max_workers=9) as pool:
            completion_future = pool.submit(_collect_sse, flood_response)
            command_futures = [
                pool.submit(
                    _request,
                    engine.port,
                    "/v1/count" if index % 2 else "/v1/tokenize",
                    body={
                        "model": engine.healthy_model,
                        "items": [f"command-{index}"],
                        "add_special": False,
                    },
                )
                for index in range(70)
            ]
            assert all(future.result()[0] == 200 for future in command_futures)
            flood_events = completion_future.result()
        flood_connection.close()
        flood_terminal = next(
            event for event in flood_events if isinstance(event, dict) and event.get("type") == "finished"
        )
        assert flood_terminal["usage"]["completion_tokens"] == 24
        assert flood_events[-1] == "[DONE]"


def test_cancel_drain_disconnect_and_recovery(tmp_path: Path) -> None:
    with _running_engine(tmp_path) as engine:
        _load_model(engine, engine.healthy_model)
        connection, response = _open_completion_stream(engine, token=INFERENCE_TOKEN)
        first = _next_sse_data(response)
        first_event = json.loads(first)
        assert first_event["type"] == "delta"
        request_id = first_event["request_id"]
        cross_status, cross_owner = _request(
            engine.port,
            "/v1/cancel",
            body={"request_id": request_id},
            token=OTHER_INFERENCE_TOKEN,
        )
        assert cross_status == 404
        assert _error_code(cross_owner) == EngineErrorCode.SESSION_UNKNOWN.value
        status, cancelled = _request(
            engine.port,
            "/v1/cancel",
            body={"request_id": request_id},
            token=INFERENCE_TOKEN,
        )
        assert status == 200, cancelled
        assert cancelled["cancelled"] is True
        events = _collect_sse(response, first)
        connection.close()
        terminal = next(event for event in events if isinstance(event, dict) and event.get("type") == "finished")
        assert terminal["finish_reason"] == "cancelled"
        assert events[-1] == "[DONE]"
        missing_status, missing = _request(
            engine.port,
            "/v1/cancel",
            body={"request_id": request_id},
            token=OTHER_INFERENCE_TOKEN,
        )
        assert (missing_status, missing) == (cross_status, cross_owner)

        connection, response = _open_completion_stream(engine)
        first = _next_sse_data(response)
        assert json.loads(first)["type"] == "delta"
        status, drain = _request(
            engine.port,
            "/admin/drain",
            body={"enabled": True},
        )
        assert status == 200 and drain["draining"] is True
        status, rejected = _request(
            engine.port,
            "/v1/completions",
            body={"model": engine.healthy_model, "prompt": "new work", "max_tokens": 1},
        )
        assert status == 503
        assert _error_code(rejected) == EngineErrorCode.DRAINING.value
        drained_events = _collect_sse(response, first)
        connection.close()
        assert any(isinstance(event, dict) and event.get("type") == "finished" for event in drained_events)
        assert drained_events[-1] == "[DONE]"

        status, undrain = _request(
            engine.port,
            "/admin/drain",
            body={"enabled": False},
        )
        assert status == 200 and undrain["draining"] is False

        disconnected, disconnected_response = _open_completion_stream(engine)
        assert disconnected_response.status == 200
        disconnected.close()
        time.sleep(0.1)
        status, sibling = _request(
            engine.port,
            "/v1/completions",
            body={
                "model": engine.healthy_model,
                "prompt": "sibling after disconnect",
                "max_tokens": 2,
                "temperature": 0.0,
                "logit_bias": {str(_forced_text_token(engine)): 100.0},
            },
            timeout=30.0,
        )
        assert status == 200, sibling
        assert sibling["completion_tokens"] > 0


@pytest.mark.parametrize(
    ("injected", "expected_status", "expected_code", "retryable", "expected_message"),
    [
        ("backend_unavailable", 503, "backend_unavailable", True, "native inference backend is unavailable"),
        ("allocation_failed", 503, "allocation_failed", True, "native allocation failed"),
        ("queue_full", 429, "queue_full", True, "engine request queue is full"),
        ("eval_timeout", 504, "eval_timeout", False, "engine evaluation timed out"),
        ("quota_exhausted", 429, "quota_exhausted", True, "engine resource quota exhausted"),
        ("cancelled", 409, "cancelled", False, "request was cancelled"),
        ("internal", 500, "internal", True, "engine request failed internally"),
        ("session_unknown", 404, "session_unknown", False, "requested session is unknown"),
        ("model_corrupt", 422, "model_corrupt", False, "model is corrupt or unreadable"),
        ("model_not_loaded", 404, "model_not_loaded", False, "requested model is not loaded"),
    ],
)
def test_typed_runtime_failures_cross_real_http_and_sse_boundaries(
    tmp_path: Path,
    injected: str,
    expected_status: int,
    expected_code: str,
    retryable: bool,
    expected_message: str,
) -> None:
    request_id = f"p15-producer-{injected}"
    trace_id = f"p15-trace-{injected}"
    headers = {
        "x-amw-test-runtime-error": injected,
        "x-request-id": request_id,
        "x-trace-id": trace_id,
    }
    with _running_engine(
        tmp_path,
        enable_test_controls=True,
    ) as engine:
        _load_model(engine, engine.healthy_model)
        body = {"model": engine.healthy_model, "prompt": "failure boundary", "max_tokens": 1}
        status, payload = _request(
            engine.port,
            "/v1/completions",
            body=body,
            request_headers=headers,
        )
        assert status == expected_status
        assert payload["schema_version"] == 1
        assert payload["error"]["code"] == expected_code
        assert payload["error"]["retryable"] is retryable
        assert payload["error"]["message"] == expected_message
        assert "contract-secret-native-detail" not in json.dumps(payload)

        connection = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=10.0)
        connection.request(
            "POST",
            "/v1/completions",
            body=json.dumps({**body, "stream": True}).encode(),
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                **headers,
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        events = _collect_sse(response)
        connection.close()
        assert len(events) == 2
        assert events[0]["schema_version"] == 1
        assert events[0]["error"]["code"] == expected_code
        assert events[0]["error"]["retryable"] is retryable
        assert events[0]["error"]["message"] == expected_message
        assert "contract-secret-native-detail" not in json.dumps(events[0])
        assert events[1] == "[DONE]"

        log_deadline = time.monotonic() + 2.0
        log_text = ""
        while time.monotonic() < log_deadline:
            if engine.log_path.is_file():
                log_text = engine.log_path.read_text(encoding="utf-8", errors="replace")
            if "generation failure mapped to sanitized public envelope" in log_text:
                break
            time.sleep(0.02)
        assert "contract-secret-native-detail" not in log_text
        assert r"C:\private\native" not in log_text
        assert "generation request failed at model-worker boundary" in log_text
        assert expected_code in log_text
        assert request_id in log_text
        assert trace_id in log_text
        assert engine.healthy_model in log_text


@pytest.mark.skipif(
    not os.environ.get("AMW_ENGINE_BINARY_WITHOUT_CONTRACT_CONTROLS"),
    reason="explicit default/release binary absence probe",
)
def test_ordinary_binary_does_not_compile_contract_mutation_controls(tmp_path: Path) -> None:
    binary = Path(os.environ["AMW_ENGINE_BINARY_WITHOUT_CONTRACT_CONTROLS"])
    with _running_engine(
        tmp_path,
        require_native_fixture=False,
        enable_test_controls=True,
        expect_test_controls=False,
        binary_path=binary,
    ) as engine:
        status, payload = _request(
            engine.port,
            "/v1/completions",
            body={"model": "absent", "prompt": "x", "max_tokens": 1},
            request_headers={"x-amw-test-runtime-error": "internal"},
        )
        assert status == 404, payload
        assert _error_code(payload) == "model_not_loaded"


def test_real_server_enforces_exact_16_mib_body_boundary_with_typed_envelope(tmp_path: Path) -> None:
    limit = 16 * 1024 * 1024
    prefix = b'{"model":"absent","prompt":"x","max_tokens":1}'
    payload = bytearray(b" " * (limit + 1))
    payload[: len(prefix)] = prefix

    with _running_engine(tmp_path, require_native_fixture=False) as engine:
        for size in (limit - 1, limit):
            connection = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=30.0)
            connection.request(
                "POST",
                "/v1/completions",
                body=memoryview(payload)[:size],
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Content-Length": str(size),
                },
            )
            response = connection.getresponse()
            decoded = json.loads(response.read())
            connection.close()
            assert response.status == 404, (size, decoded)
            assert decoded["error"]["code"] == "model_not_loaded"

        connection = socket.create_connection(("127.0.0.1", engine.port), timeout=5.0)
        connection.sendall(
            (
                "POST /v1/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{engine.port}\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {limit + 1}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        raw = bytearray()
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            raw.extend(chunk)
        connection.close()
        head, body = bytes(raw).split(b"\r\n\r\n", maxsplit=1)
        assert b" 413 " in head
        decoded = json.loads(body)
        assert decoded == {
            "schema_version": 1,
            "error": {
                "code": "context_overflow",
                "message": "request body exceeds 16 MiB",
                "retryable": False,
            },
        }


def test_chunked_body_limit_uses_the_same_exact_typed_413_boundary(tmp_path: Path) -> None:
    limit = 16 * 1024 * 1024
    prefix = b'{"model":"absent","prompt":"x","max_tokens":1}'
    payload = bytearray(b" " * (limit + 2))
    payload[: len(prefix)] = prefix

    def chunked_request(port: int, size: int) -> tuple[bytes, dict[str, Any]]:
        client = socket.create_connection(("127.0.0.1", port), timeout=30.0)
        client.sendall(
            (
                "POST /v1/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                "Content-Type: application/json\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Connection: close\r\n\r\n"
                f"{size:x}\r\n"
            ).encode("ascii")
        )
        client.sendall(memoryview(payload)[:size])
        with suppress(BrokenPipeError, ConnectionResetError):
            client.sendall(b"\r\n0\r\n\r\n")
        raw = bytearray()
        while True:
            try:
                chunk = client.recv(4096)
            except ConnectionResetError:
                break
            if not chunk:
                break
            raw.extend(chunk)
        client.close()
        head, body = bytes(raw).split(b"\r\n\r\n", maxsplit=1)
        return head, json.loads(body)

    with _running_engine(tmp_path, require_native_fixture=False) as engine:
        for size in (limit - 1, limit):
            head, decoded = chunked_request(engine.port, size)
            assert b" 404 " in head, (size, decoded)
            assert _error_code(decoded) == "model_not_loaded"

        for size in (limit + 1, limit + 2):
            head, decoded = chunked_request(engine.port, size)
            assert b" 413 " in head, (size, decoded)
            assert decoded == {
                "schema_version": 1,
                "error": {
                    "code": "context_overflow",
                    "message": "request body exceeds 16 MiB",
                    "retryable": False,
                },
            }

        malformed = socket.create_connection(("127.0.0.1", engine.port), timeout=5.0)
        malformed.sendall(
            (
                "POST /v1/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{engine.port}\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                "Content-Type: application/json\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Connection: close\r\n\r\n"
                "1\r\n"
            ).encode("ascii")
        )
        malformed.shutdown(socket.SHUT_WR)
        malformed_raw = bytearray()
        while chunk := malformed.recv(4096):
            malformed_raw.extend(chunk)
        malformed.close()
        malformed_head, malformed_body = bytes(malformed_raw).split(b"\r\n\r\n", maxsplit=1)
        assert b" 422 " in malformed_head
        assert json.loads(malformed_body) == {
            "schema_version": 1,
            "error": {
                "code": "unsupported_param",
                "message": "request body could not be read",
                "retryable": False,
            },
        }

        status, recovered = _request(
            engine.port,
            "/v1/completions",
            body={"model": "absent", "prompt": "x", "max_tokens": 1},
        )
        assert status == 404, recovered
        assert _error_code(recovered) == "model_not_loaded"


def test_concurrent_body_admission_caps_aggregate_bytes_before_body_allocation(tmp_path: Path) -> None:
    limit = 16 * 1024 * 1024

    def open_stalled_body(port: int) -> socket.socket:
        client = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        client.sendall(
            (
                "POST /v1/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {limit}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        return client

    with _running_engine(tmp_path, require_native_fixture=False) as engine:
        stalled = [open_stalled_body(engine.port) for _ in range(2)]
        try:
            time.sleep(0.1)
            refused = open_stalled_body(engine.port)
            refused.settimeout(5.0)
            raw = bytearray()
            while True:
                chunk = refused.recv(4096)
                if not chunk:
                    break
                raw.extend(chunk)
            refused.close()
            head, body = bytes(raw).split(b"\r\n\r\n", maxsplit=1)
            assert b" 429 " in head
            assert b"retry-after: 1" in head.lower()
            payload = json.loads(body)
            assert payload == {
                "schema_version": 1,
                "error": {
                    "code": "quota_exhausted",
                    "message": "engine HTTP admission capacity is exhausted",
                    "retryable": True,
                },
            }
        finally:
            for client in stalled:
                client.close()

        status, recovered = _request(
            engine.port,
            "/v1/completions",
            body={"model": "absent", "prompt": "x", "max_tokens": 1},
        )
        assert status == 404, recovered
        assert _error_code(recovered) == "model_not_loaded"


def test_slowloris_bodies_expire_and_release_all_request_permits(tmp_path: Path) -> None:
    def open_slow_body(port: int) -> socket.socket:
        client = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        client.sendall(
            (
                "POST /v1/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 1\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        return client

    with _running_engine(
        tmp_path,
        require_native_fixture=False,
        enable_test_controls=True,
        body_read_timeout_ms=2_000,
    ) as engine:
        slow_clients = [open_slow_body(engine.port) for _ in range(64)]
        refused = open_slow_body(engine.port)
        refused.settimeout(5.0)
        refused_raw = bytearray()
        while chunk := refused.recv(4096):
            refused_raw.extend(chunk)
        refused.close()
        refused_head, refused_body = bytes(refused_raw).split(b"\r\n\r\n", maxsplit=1)
        assert b" 429 " in refused_head
        assert json.loads(refused_body)["error"]["code"] == "quota_exhausted"

        slow_clients[0].settimeout(5.0)
        timed_out_raw = bytearray()
        while chunk := slow_clients[0].recv(4096):
            timed_out_raw.extend(chunk)
        timeout_head, timeout_body = bytes(timed_out_raw).split(b"\r\n\r\n", maxsplit=1)
        assert b" 408 " in timeout_head
        assert json.loads(timeout_body) == {
            "schema_version": 1,
            "error": {
                "code": "unsupported_param",
                "message": "request body was not received before the read deadline",
                "retryable": True,
            },
        }
        for client in slow_clients:
            client.close()

        status, recovered = _request(
            engine.port,
            "/v1/completions",
            body={"model": "absent", "prompt": "x", "max_tokens": 1},
        )
        assert status == 404, recovered
        assert _error_code(recovered) == "model_not_loaded"


def test_authenticated_slow_event_consumer_reports_exact_lag_and_replays_in_order(tmp_path: Path) -> None:
    injected_count = 4098
    with _running_engine(
        tmp_path,
        require_native_fixture=False,
        enable_test_controls=True,
    ) as engine:
        slow = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=10.0)
        slow.request(
            "GET",
            "/events",
            headers={
                "Authorization": f"Bearer {OBSERVABILITY_TOKEN}",
                "Accept": "application/x-ndjson",
                "x-amw-test-event-pause-ms": "750",
            },
        )
        slow_response = slow.getresponse()
        assert slow_response.status == 200
        generation = slow_response.headers["x-engine-event-generation"]
        assert slow_response.headers["x-engine-event-start-cursor"] == "0"

        injector = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=10.0)
        injector.request(
            "GET",
            "/events",
            headers={
                "Authorization": f"Bearer {OBSERVABILITY_TOKEN}",
                "Accept": "application/x-ndjson",
                "x-amw-test-event-count": str(injected_count),
            },
        )
        injected_response = injector.getresponse()
        assert injected_response.status == 200
        injector.close()

        lag = json.loads(slow_response.readline())
        slow.close()
        assert lag == {
            "transport_error": {
                "code": "lagged",
                "missed": 2,
                "resume_after": 2,
            }
        }

        replay = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=10.0)
        replay.request(
            "GET",
            f"/events?generation={generation}&after_cursor=2",
            headers={
                "Authorization": f"Bearer {OBSERVABILITY_TOKEN}",
                "Accept": "application/x-ndjson",
            },
        )
        replay_response = replay.getresponse()
        assert replay_response.status == 200
        assert replay_response.headers["x-engine-event-start-cursor"] == "2"
        first = [json.loads(replay_response.readline()) for _ in range(3)]
        replay.close()
        assert [event["ts"] for event in first] == [1_000_002.0, 1_000_003.0, 1_000_004.0]
        assert all(event["event"] == "gauges" for event in first)


def test_stop_string_cancel_boundary_has_one_terminal_and_preserves_sibling(tmp_path: Path) -> None:
    request_id = "stop-cancel-boundary"
    with _running_engine(tmp_path, enable_test_controls=True) as engine:
        _load_model(engine, engine.healthy_model)
        forced_token = _forced_text_token(engine)
        status, sample = _request(
            engine.port,
            "/v1/completions",
            body={
                "model": engine.healthy_model,
                "prompt": "token boundary",
                "max_tokens": 1,
                "temperature": 0.0,
                "logit_bias": {str(forced_token): 100.0},
            },
            timeout=30.0,
        )
        assert status == 200, sample
        stop_text = sample["text"]
        assert stop_text

        for iteration in range(3):
            sibling_connection, sibling_response = _open_completion_stream(
                engine,
                max_tokens=8,
                forced_token=forced_token,
                request_id=f"stop-cancel-sibling-{iteration}",
            )
            target_connection, target_response = _open_completion_stream(
                engine,
                max_tokens=32,
                forced_token=forced_token,
                stop=[stop_text],
                request_id=request_id,
                token=INFERENCE_TOKEN,
            )
            cancel_status, cancel_payload = _request(
                engine.port,
                "/v1/cancel",
                body={"request_id": request_id},
                token=INFERENCE_TOKEN,
            )
            assert cancel_status == 200, cancel_payload
            assert cancel_payload["cancelled"] is True
            target_events = _collect_sse(target_response)
            sibling_events = _collect_sse(sibling_response)
            target_connection.close()
            sibling_connection.close()

            terminals = [
                event for event in target_events if isinstance(event, dict) and event.get("type") == "finished"
            ]
            assert len(terminals) == 1
            assert terminals[0]["finish_reason"] == "stop"
            assert target_events[-1] == "[DONE]"
            assert not any(
                stop_text in event.get("delta", "")
                for event in target_events
                if isinstance(event, dict) and event.get("type") == "delta"
            )
            terminal_index = target_events.index(terminals[0])
            assert not any(
                isinstance(event, dict) and event.get("type") == "delta"
                for event in target_events[terminal_index + 1 :]
            )
            sibling_terminal = next(
                event for event in sibling_events if isinstance(event, dict) and event.get("type") == "finished"
            )
            assert sibling_terminal["usage"]["completion_tokens"] == 8
            assert sibling_events[-1] == "[DONE]"


@pytest.mark.timeout(180)
def test_governed_fim_model_completes_public_infill_and_stops_at_exact_limit(tmp_path: Path) -> None:
    fixture = _native_fim_fixture()
    with _running_engine(tmp_path, native_fixture=fixture, ram_gb=1.0) as engine:
        # Native model loading competes with the other deterministic shards in
        # the engine-contract boundary. Keep the operation bounded while
        # allowing that intentional CPU/RAM contention to clear.
        loaded = _load_model(engine, engine.healthy_model, timeout=120.0)["model"]
        assert loaded["supports_fim"] is True
        forced_token = _forced_text_token(engine)

        status, infill = _request(
            engine.port,
            "/v1/infill",
            body={
                "model": engine.healthy_model,
                "prompt": "def answer():\n    ",
                "suffix": "\n",
                "max_tokens": 2,
                "temperature": 0.0,
                "logit_bias": {str(forced_token): 100.0},
            },
            timeout=30.0,
        )

        assert status == 200, infill
        assert infill["text"], "native FIM must return generated bytes, not a capability-only success"
        assert infill["prompt_tokens"] >= 3, "the native prompt must include all three FIM sentinels"
        assert infill["completion_tokens"] == 2
        assert infill["finish_reason"] == "length"
