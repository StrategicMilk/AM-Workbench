#!/usr/bin/env python3
"""Prepare the governed real-process AM Engine contract-test environment."""

from __future__ import annotations

import atexit
import csv
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from scripts.verify_engine_model_fixture import (
    FIM_MODEL_SHA256,
    FIM_MODEL_SIZE,
    MODEL_SHA256,
    MODEL_SIZE,
    ensure_fixture,
)

NATIVE_FIXTURE_NAME = "tinyllama-15M-stories-Q2_K.gguf"
FIM_FIXTURE_NAME = "qwen2.5-coder-0.5b-instruct-q2_k.gguf"
WINDOWS_SYSTEM_SID = "S-1-5-18"

_WINDOWS_PRIVATE_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:AMW_CONTRACT_PRIVATE_PATH
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User
$verified = Get-Acl -LiteralPath $path
$entries = @($verified.Access | ForEach-Object {
    [pscustomobject]@{
        sid = $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
        type = $_.AccessControlType.ToString()
        inherited = $_.IsInherited
        rights = $_.FileSystemRights.ToString()
    }
})
[pscustomobject]@{
    owner_sid = $verified.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    current_sid = $sid.Value
    protected = $verified.AreAccessRulesProtected
    entries = $entries
} | ConvertTo-Json -Compress -Depth 4
"""


def _acl_entries(state: dict[str, object]) -> list[dict[str, object]]:
    raw_entries = state.get("entries", [])
    if isinstance(raw_entries, dict):
        raw_entries = [raw_entries]
    if not isinstance(raw_entries, list) or any(not isinstance(entry, dict) for entry in raw_entries):
        raise RuntimeError("private engine contract DACL inspection returned invalid entries")
    return raw_entries


def _is_owner_full_control_entry(entry: dict[str, object], owner_sid: str) -> bool:
    rights = entry.get("rights")
    return (
        entry.get("sid") == owner_sid
        and entry.get("type") == "Allow"
        and entry.get("inherited") is False
        and isinstance(rights, str)
        and "FullControl" in rights
    )


def _is_owner_only_acl_state(state: dict[str, object], expected_owner_sid: str) -> bool:
    try:
        entries = _acl_entries(state)
    except RuntimeError:
        return False
    verified_sid = state.get("current_sid")
    return (
        state.get("protected") is True
        and verified_sid == expected_owner_sid
        and state.get("owner_sid") == expected_owner_sid
        and bool(entries)
        and all(_is_owner_full_control_entry(entry, expected_owner_sid) for entry in entries)
    )


def _is_engine_session_acl_state(state: dict[str, object], expected_owner_sid: str) -> bool:
    """Return whether a DACL matches the Rust session-store contract exactly."""
    try:
        entries = _acl_entries(state)
    except RuntimeError:
        return False
    allowed_sids = {expected_owner_sid, WINDOWS_SYSTEM_SID}
    return (
        state.get("protected") is True
        and state.get("current_sid") == expected_owner_sid
        and state.get("owner_sid") == expected_owner_sid
        and len(entries) == len(allowed_sids)
        and {entry.get("sid") for entry in entries} == allowed_sids
        and all(_is_owner_full_control_entry(entry, str(entry.get("sid"))) for entry in entries)
    )


def _find_libclang(root: Path, env: dict[str, str]) -> Path:
    configured = Path(env["LIBCLANG_PATH"]) if env.get("LIBCLANG_PATH") else None
    candidates = [
        configured,
        root / ".venv312" / "Lib" / "site-packages" / "clang" / "native",
        *root.glob(".venv312/lib/python*/site-packages/clang/native"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_dir() and any(candidate.glob("libclang*")):
            return candidate.resolve()
    raise RuntimeError("engine contract validation requires governed LIBCLANG_PATH or the project clang package")


def _server_path(target_dir: Path) -> Path:
    executable = "amw-engine-server.exe" if os.name == "nt" else "amw-engine-server"
    return target_dir / "debug" / executable


def _restrict_private_directory(path: Path, env: dict[str, str]) -> str:
    """Establish and verify an owner-only, non-inheriting per-run directory."""
    if os.name != "nt":
        os.chmod(path, 0o700)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError(f"engine contract run directory is not private: {path}")
        return str(path.stat().st_uid)

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    icacls = shutil.which("icacls.exe") or shutil.which("icacls")
    whoami = shutil.which("whoami.exe") or shutil.which("whoami")
    if powershell is None or icacls is None or whoami is None:
        raise RuntimeError("engine contract validation requires Windows ACL tooling")
    acl_env = os.environ.copy()
    acl_env.update(env)
    acl_env["AMW_CONTRACT_PRIVATE_PATH"] = str(path)
    identity = subprocess.run(
        [whoami, "/user", "/fo", "csv", "/nh"],
        env=acl_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    if identity.returncode != 0:
        raise RuntimeError(f"unable to identify the engine contract directory owner: {identity.stderr.strip()}")
    try:
        current_sid = next(csv.reader(io.StringIO(identity.stdout)))[-1]
    except (IndexError, StopIteration) as exc:
        raise RuntimeError("unable to parse the engine contract directory owner SID") from exc
    acl_update = subprocess.run(
        [icacls, str(path), "/inheritance:r", "/grant:r", f"*{current_sid}:(OI)(CI)F"],
        env=acl_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if acl_update.returncode != 0:
        raise RuntimeError(f"unable to establish private engine contract DACL: {acl_update.stderr.strip()}")
    owner_update = subprocess.run(
        [icacls, str(path), "/setowner", f"*{current_sid}"],
        env=acl_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if owner_update.returncode != 0:
        raise RuntimeError(f"unable to set private engine contract directory owner: {owner_update.stderr.strip()}")

    def read_acl_state() -> dict[str, object]:
        result = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_PRIVATE_ACL_SCRIPT],
            env=acl_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"unable to inspect private engine contract DACL: {result.stderr.strip()}")
        try:
            state = json.loads(result.stdout.strip())
        except json.JSONDecodeError as exc:
            raise RuntimeError("unable to verify private engine contract DACL") from exc
        if not isinstance(state, dict):
            raise RuntimeError("private engine contract DACL inspection returned an invalid record")
        return state

    state = read_acl_state()
    initial_entries = _acl_entries(state)
    for entry in initial_entries:
        sid = entry.get("sid")
        if not sid or sid == current_sid:
            continue
        removal = subprocess.run(
            [icacls, str(path), "/remove:g", f"*{sid}"],
            env=acl_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if removal.returncode != 0:
            raise RuntimeError(f"unable to remove non-owner engine contract DACL entry {sid}: {removal.stderr.strip()}")

    state = read_acl_state()
    if not _is_owner_only_acl_state(state, current_sid):
        raise RuntimeError(f"engine contract run directory DACL is not owner-only: {state!r}")
    return current_sid


def prepare_private_directory(path: Path, env: dict[str, str] | None = None) -> str:
    """Create an external engine directory with its exact privacy contract.

    The Rust session store accepts precisely two explicit full-control ACEs on
    Windows: the current owner and ``SYSTEM``. POSIX uses owner-only mode 0700.
    The runtime intentionally treats external roots as verify-only, so the
    contract harness must establish this descriptor before engine startup.
    """
    selected_env = os.environ.copy() if env is None else env.copy()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    current_owner = _restrict_private_directory(path, selected_env)
    if os.name != "nt":
        return current_owner

    icacls = shutil.which("icacls.exe") or shutil.which("icacls")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if icacls is None or powershell is None:
        raise RuntimeError("engine contract validation requires Windows ACL tooling")
    acl_env = os.environ.copy()
    acl_env.update(selected_env)
    acl_env["AMW_CONTRACT_PRIVATE_PATH"] = str(path)
    system_grant = subprocess.run(
        [icacls, str(path), "/grant:r", f"*{WINDOWS_SYSTEM_SID}:(OI)(CI)F"],
        env=acl_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if system_grant.returncode != 0:
        raise RuntimeError(f"unable to grant SYSTEM access to engine session directory: {system_grant.stderr.strip()}")
    inspection = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_PRIVATE_ACL_SCRIPT],
        env=acl_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if inspection.returncode != 0:
        raise RuntimeError(f"unable to inspect engine session DACL: {inspection.stderr.strip()}")
    try:
        state = json.loads(inspection.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("unable to verify engine session DACL") from exc
    if not isinstance(state, dict) or not _is_engine_session_acl_state(state, current_owner):
        raise RuntimeError(f"engine session directory DACL does not match the runtime contract: {state!r}")
    return current_owner


def _file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def verify_engine_contract_path(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> Path:
    """Fail closed unless a prepared contract input retains its exact identity."""
    resolved = path.resolve()
    if not resolved.is_file():
        raise RuntimeError(f"engine contract input is missing: {resolved}")
    observed_sha256, observed_size = _file_identity(resolved)
    if observed_size != expected_size or observed_sha256 != expected_sha256:
        raise RuntimeError(
            "engine contract input identity changed after preparation: "
            f"{resolved} (size={observed_size}, sha256={observed_sha256})"
        )
    return resolved


def _materialize_verified(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    executable: bool = False,
) -> Path:
    """Atomically copy one verified input into the private per-run directory."""
    verify_engine_contract_path(
        source,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.chmod(temporary, 0o700 if executable else 0o600)
        verify_engine_contract_path(
            temporary,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return verify_engine_contract_path(
        destination,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    )


def _build_server(root: Path, target_dir: Path, features: str, env: dict[str, str]) -> Path:
    build_env = env.copy()
    build_env["CARGO_TARGET_DIR"] = str(target_dir)
    command = [
        "cargo",
        "build",
        "-p",
        "amw-engine",
        "--features",
        features,
        "--bin",
        "amw-engine-server",
        "--locked",
    ]
    result = subprocess.run(
        command,
        cwd=root,
        env=build_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"engine contract build failed for features {features!r} with exit {result.returncode}")
    binary = _server_path(target_dir)
    if not binary.is_file():
        raise RuntimeError(f"engine contract build did not produce {binary}")
    return binary.resolve()


def prepare_engine_contract_environment(root: Path, base_env: dict[str, str]) -> dict[str, str]:
    """Provision governed fixtures and isolated ordinary/control binaries.

    Args:
        root: Repository root.
        base_env: Environment inherited by the validation command.

    Returns:
        A copied environment containing exact fixture and binary paths.

    Raises:
        RuntimeError: If governed inputs or either required binary cannot be prepared.
    """
    root = root.resolve()
    env = base_env.copy()
    env["LIBCLANG_PATH"] = str(_find_libclang(root, env))
    runtime_root = root / ".vetinari" / "test-runtime" / "engine-contract"
    cache_dir = runtime_root / "cache"
    native_cache = cache_dir / "fixtures" / "native" / NATIVE_FIXTURE_NAME
    fim_cache = cache_dir / "fixtures" / "fim" / FIM_FIXTURE_NAME

    print("preparing governed native AM Engine fixture", flush=True)
    ensure_fixture(native_cache, native_cache.parent / "evidence", fixture="native")
    print("preparing governed FIM AM Engine fixture", flush=True)
    ensure_fixture(fim_cache, fim_cache.parent / "evidence", fixture="fim")

    print("building isolated ordinary AM Engine server", flush=True)
    ordinary_cache = _build_server(root, cache_dir / "target-ordinary", "cpu", env)
    print("building isolated contract-control AM Engine server", flush=True)
    control_cache = _build_server(
        root,
        cache_dir / "target-controls",
        "cpu,contract-test-controls",
        env,
    )

    ordinary_sha256, ordinary_size = _file_identity(ordinary_cache)
    control_sha256, control_size = _file_identity(control_cache)
    run_dir = runtime_root / "runs" / uuid.uuid4().hex
    run_dir.mkdir(parents=True, mode=0o700)
    private_owner = _restrict_private_directory(run_dir, env)
    atexit.register(shutil.rmtree, run_dir, True)

    native_model = _materialize_verified(
        native_cache,
        run_dir / "fixtures" / MODEL_SHA256 / NATIVE_FIXTURE_NAME,
        expected_sha256=MODEL_SHA256,
        expected_size=MODEL_SIZE,
    )
    fim_model = _materialize_verified(
        fim_cache,
        run_dir / "fixtures" / FIM_MODEL_SHA256 / FIM_FIXTURE_NAME,
        expected_sha256=FIM_MODEL_SHA256,
        expected_size=FIM_MODEL_SIZE,
    )
    ordinary_binary = _materialize_verified(
        ordinary_cache,
        run_dir / "binaries" / ordinary_sha256 / ordinary_cache.name,
        expected_sha256=ordinary_sha256,
        expected_size=ordinary_size,
        executable=True,
    )
    control_binary = _materialize_verified(
        control_cache,
        run_dir / "binaries" / control_sha256 / control_cache.name,
        expected_sha256=control_sha256,
        expected_size=control_size,
        executable=True,
    )

    env.update({
        "AMW_ENGINE_CONTRACT_RUN_DIR": str(run_dir),
        "AMW_ENGINE_CONTRACT_PRIVATE_OWNER": private_owner,
        "AMW_ENGINE_NATIVE_TEST_MODEL": str(native_model.resolve()),
        "AMW_ENGINE_NATIVE_TEST_MODEL_SHA256": MODEL_SHA256,
        "AMW_ENGINE_NATIVE_TEST_MODEL_SIZE": str(MODEL_SIZE),
        "AMW_ENGINE_NATIVE_FIM_TEST_MODEL": str(fim_model.resolve()),
        "AMW_ENGINE_NATIVE_FIM_TEST_MODEL_SHA256": FIM_MODEL_SHA256,
        "AMW_ENGINE_NATIVE_FIM_TEST_MODEL_SIZE": str(FIM_MODEL_SIZE),
        "AMW_ENGINE_BINARY_WITHOUT_CONTRACT_CONTROLS": str(ordinary_binary),
        "AMW_ENGINE_BINARY_WITHOUT_CONTRACT_CONTROLS_SHA256": ordinary_sha256,
        "AMW_ENGINE_BINARY_WITHOUT_CONTRACT_CONTROLS_SIZE": str(ordinary_size),
        "AMW_ENGINE_CONTRACT_BINARY": str(control_binary),
        "AMW_ENGINE_CONTRACT_BINARY_SHA256": control_sha256,
        "AMW_ENGINE_CONTRACT_BINARY_SIZE": str(control_size),
    })
    return env


__all__ = ["prepare_engine_contract_environment", "verify_engine_contract_path"]
