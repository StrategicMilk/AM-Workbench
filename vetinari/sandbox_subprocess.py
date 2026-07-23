"""Subprocess utility functions for the code sandbox."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from vetinari.security.fail_closed import UntrustedInputError, sanitize_untrusted_text

logger = logging.getLogger(__name__)

_MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def wrap_python_code(
    code: str,
    input_data: dict[str, Any] | None = None,
    blocked_modules: list[str] | None = None,
    allow_network: bool = False,
    filesystem_allowlist: list[str] | None = None,
) -> str:
    """Wrap user Python code with the Vetinari sandbox harness.

    Args:
        code: Code value consumed by wrap_python_code().
        input_data: Structured data consumed by the operation.
        blocked_modules: Blocked modules value consumed by wrap_python_code().
        allow_network: Allow network value consumed by wrap_python_code().
        filesystem_allowlist: File path or file-like value consumed by the operation.

    Returns:
        Value produced for the caller.
    """
    import base64 as _b64

    code = _validate_code_text(code)
    input_data = _validate_input_data(input_data)
    blocked_modules = _validate_blocked_modules(blocked_modules)
    filesystem_allowlist = _validate_filesystem_allowlist(filesystem_allowlist)

    code_b64 = _b64.b64encode(code.encode("utf-8")).decode("ascii")
    return "\n".join((
        _sandbox_header(input_data, blocked_modules, allow_network, filesystem_allowlist),
        _sandbox_import_guard(),
        _sandbox_filesystem_guard(),
        _sandbox_capture_block(),
        _sandbox_execution_block(code_b64),
    ))


def _validate_code_text(code: object) -> str:
    if not isinstance(code, str):
        raise UntrustedInputError("sandbox code must be a string")
    if not code.strip():
        raise UntrustedInputError("sandbox code is empty")
    if len(code) > 200_000:
        raise UntrustedInputError("sandbox code exceeds maximum length")
    return code


def _validate_input_data(input_data: object) -> dict[str, Any] | None:
    if input_data is None:
        return None
    if not isinstance(input_data, dict):
        raise UntrustedInputError("sandbox input_data must be an object")
    json.dumps(input_data)
    return input_data


def _validate_blocked_modules(blocked_modules: object) -> list[str] | None:
    if blocked_modules is None:
        return None
    if not isinstance(blocked_modules, list):
        raise UntrustedInputError("blocked_modules must be a list")
    validated: list[str] = []
    for module in blocked_modules:
        text = sanitize_untrusted_text(module, max_length=128)
        if not _MODULE_NAME_RE.fullmatch(text):
            raise UntrustedInputError(f"invalid blocked module name: {module!r}")
        validated.append(text)
    return validated


def _validate_filesystem_allowlist(filesystem_allowlist: object) -> list[str] | None:
    if filesystem_allowlist is None:
        return None
    if not isinstance(filesystem_allowlist, list):
        raise UntrustedInputError("filesystem_allowlist must be a list")
    return [sanitize_untrusted_text(path, max_length=1024) for path in filesystem_allowlist]


def _sandbox_header(
    input_data: dict[str, Any] | None,
    blocked_modules: list[str] | None,
    allow_network: bool,
    filesystem_allowlist: list[str] | None,
) -> str:
    """Return wrapper imports and serialized sandbox inputs."""
    input_json = json.dumps(input_data or {})
    blocked_json = json.dumps(blocked_modules or [])
    allow_network_str = "True" if allow_network else "False"
    fs_allowlist_json = json.dumps(filesystem_allowlist or [])
    return f"""
import sys as _sys
import json as _json
import traceback as _tb
import base64 as _b64
import builtins as _builtins
import pathlib as _pathlib

_real_stdout = _sys.stdout
_real_stderr = _sys.stderr

INPUT_DATA = {input_json}
_BLOCKED_MODULES = {blocked_json}
_ALLOW_NETWORK = {allow_network_str}
_FS_ALLOWLIST = {fs_allowlist_json}
"""


def _sandbox_import_guard() -> str:
    """Return wrapper code that blocks disallowed imports."""
    return r"""
_WRAPPER_NEEDS = {"sys", "json", "traceback", "base64", "builtins"}
_original_import = _builtins.__import__

def _restricted_import(name, *args, **kwargs):
    top_level = name.split(".")[0]
    if top_level in _BLOCKED_MODULES:
        raise ImportError("Module %r is blocked in the Vetinari sandbox" % name)
    if not _ALLOW_NETWORK and top_level in ("socket", "requests", "urllib", "httpx", "aiohttp"):
        raise ImportError("Network module %r is blocked (allow_network=False)" % name)
    return _original_import(name, *args, **kwargs)

_builtins.__import__ = _restricted_import
for _mod in list(_sys.modules):
    _top = _mod.split(".")[0]
    if _top in _BLOCKED_MODULES and _top not in _WRAPPER_NEEDS:
        del _sys.modules[_mod]
"""


def _sandbox_filesystem_guard() -> str:
    """Return wrapper code that enforces filesystem allowlist policy."""
    return r"""
_original_open = _builtins.open
_original_path_open = _pathlib.Path.open
_FS_ALLOWLIST_PATHS = [_pathlib.Path(_prefix).resolve() for _prefix in _FS_ALLOWLIST]
_PYTHON_PREFIX = str(_sys.prefix).replace("\\", "/")
_PYTHON_BASE_PREFIX = str(_sys.base_prefix).replace("\\", "/")

def _is_allowlisted(_resolved_path):
    if not _FS_ALLOWLIST_PATHS:
        return False
    return any(_resolved_path.is_relative_to(_prefix) for _prefix in _FS_ALLOWLIST_PATHS)

def _restricted_open(file, mode="r", *args, **kwargs):
    _is_write = any(m in str(mode) for m in ("w", "a", "x")) or "+" in str(mode)
    from pathlib import Path as _Path
    _resolved_path = _Path(file).resolve()
    _resolved = str(_resolved_path)
    _resolved_fwd = _resolved.replace("\\", "/")
    if _is_write:
        if _FS_ALLOWLIST and not _is_allowlisted(_resolved_path):
            raise PermissionError("Write access to path %r is blocked by the Vetinari sandbox filesystem allowlist" % _resolved)
        if not _FS_ALLOWLIST:
            raise PermissionError("Write access to path %r is blocked - no filesystem allowlist configured for this sandbox" % _resolved)
    elif _FS_ALLOWLIST:
        _is_python = _resolved_fwd.startswith(_PYTHON_PREFIX) or _resolved_fwd.startswith(_PYTHON_BASE_PREFIX)
        if not _is_python and not _is_allowlisted(_resolved_path):
            raise PermissionError("Read access to path %r is blocked by the Vetinari sandbox filesystem allowlist" % _resolved)
    return _original_open(file, mode, *args, **kwargs)

_builtins.open = _restricted_open

def _restricted_path_open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
    return _restricted_open(self, mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline)

def _restricted_path_read_text(self, encoding=None, errors=None, newline=None):
    with _restricted_open(self, "r", encoding=encoding, errors=errors, newline=newline) as _fh:
        return _fh.read()

def _restricted_path_write_text(self, data, encoding=None, errors=None, newline=None):
    with _restricted_open(self, "w", encoding=encoding, errors=errors, newline=newline) as _fh:
        return _fh.write(data)

_pathlib.Path.open = _restricted_path_open
_pathlib.Path.read_text = _restricted_path_read_text
_pathlib.Path.write_text = _restricted_path_write_text
"""


def _sandbox_capture_block() -> str:
    """Return wrapper code for stdout/stderr capture."""
    return r"""
_output = []
_errors = []

class _OutputCapture:
    def write(self, text):
        if text.strip():
            _output.append(text)
    def flush(self):
        pass

_sys.stdout = _OutputCapture()
_sys.stderr = _OutputCapture()
"""


def _sandbox_execution_block(code_b64: str) -> str:
    """Return wrapper code that executes user code and emits JSON output."""
    return f'''
_user_code = _b64.b64decode("{code_b64}").decode("utf-8")
try:
    _sandbox_globals = {{}}
    _code_obj = compile(_user_code, "<vetinari_sandbox>", "exec")
    _builtins.eval(_code_obj, _sandbox_globals)
except Exception:
    _errors.append(_tb.format_exc())

_sys.stdout = _real_stdout
_sys.stderr = _real_stderr

_result = {{
    "success": len(_errors) == 0,
    "output": "".join(_output),
    "errors": "".join(_errors),
    "input_received": INPUT_DATA,
}}

_real_stdout.write("===VETINARI_OUTPUT_START===\\n")
_real_stdout.write(_json.dumps(_result) + "\\n")
_real_stdout.write("===VETINARI_OUTPUT_END===\\n")
'''


def parse_sandbox_output(raw_stdout: str) -> tuple[bool | None, str, str]:
    """Extract success status from the structured wrapper output block.

    Returns:
        Value produced for the caller.
    """
    start_marker = "===VETINARI_OUTPUT_START==="
    end_marker = "===VETINARI_OUTPUT_END==="
    start_idx = raw_stdout.find(start_marker)
    end_idx = raw_stdout.find(end_marker)
    if start_idx == -1 or end_idx == -1:
        return None, raw_stdout, ""
    json_text = raw_stdout[start_idx + len(start_marker) : end_idx].strip()
    try:
        data = json.loads(json_text)
        return (
            bool(data.get("success", False)),
            data.get("output", ""),
            data.get("errors", ""),
        )
    except (json.JSONDecodeError, ValueError):
        logger.warning("Sandbox output is not JSON - returning raw stdout as plain text output")
        return None, raw_stdout, ""
