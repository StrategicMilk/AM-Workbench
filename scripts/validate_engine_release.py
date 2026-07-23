#!/usr/bin/env python3
"""Validate AM Engine archives, release manifests, and publication evidence."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

# Keep this release publisher self-contained.  The source-free publish job
# intentionally downloads only this reviewed script, so even a convenience
# import from the installed Vetinari package or the runner's global site-packages
# would turn publication into an undeclared supply-chain dependency.
EXPORT_TOOL_MEMBERS = {
    "convert_hf_to_gguf": "convert_hf_to_gguf.py",
    "convert_lora_to_gguf": "convert_lora_to_gguf.py",
    "imatrix": "llama-imatrix",
    "quantize": "llama-quantize",
}
EXPORT_NATIVE_TOOLS = frozenset({"imatrix", "quantize"})


def export_tool_member(tool: str, *, platform: str) -> str:
    """Return one immutable export member without importing project code."""
    if platform not in {"linux", "windows"}:
        raise ValueError(f"unsupported AM Engine bundle platform: {platform!r}")
    try:
        member = EXPORT_TOOL_MEMBERS[tool]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unsupported AM Engine export tool: {tool!r}") from exc
    return f"{member}.exe" if platform == "windows" and tool in EXPORT_NATIVE_TOOLS else member


def export_tool_members(*, platform: str) -> frozenset[str]:
    """Return every immutable export member without importing project code."""
    return frozenset(export_tool_member(tool, platform=platform) for tool in EXPORT_TOOL_MEMBERS)


EXPECTED_LEGS = {
    "amw-engine-windows-cpu.zip": ("windows", "cpu"),
    "amw-engine-windows-cuda.zip": ("windows", "cuda"),
    "amw-engine-linux-cpu.zip": ("linux", "cpu"),
    "amw-engine-linux-cuda.zip": ("linux", "cuda"),
}
SIZE_LEDGER_KEYS = tuple(name.removeprefix("amw-engine-").removesuffix(".zip") for name in EXPECTED_LEGS)
SPDX_MEMBER = "ENGINE_LICENSES/SPDX.spdx.json"
LICENSE_INDEX_MEMBER = "ENGINE_LICENSES/INDEX.json"
MAX_ARCHIVE_MEMBERS = 10_000
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
CUDA_CERTIFICATION_LABELS = (
    "self-hosted",
    "vetinari-engine-gpu",
    "linux",
    "x64",
    "cuda-12-4",
)
CUDA_TRAINING_RUNNER_LABEL = "vetinari-training-gpu"
CUDA_CERTIFICATION_JOB_NAME = "Certify CUDA runtime and model offload on governed GPU hardware"
CUDA_FIXTURE_SHA256 = "f7e39dc9f26f3d39bf59e885349c6eec65880f685322d591f53e6cdb46ceb2e9"
CUDA_CERTIFICATION_ASSET = "cuda-certification.json"
CONSUMER_AUTHORITY_ASSET = "consumer-release-authority.json"
FIM_MODEL_REVISION = "ebb2015119c907b064c512bf053e945850b5875f"
FIM_MODEL_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/"
    f"{FIM_MODEL_REVISION}/qwen2.5-coder-0.5b-instruct-q2_k.gguf"
)
FIM_MODEL_SHA256 = "f9bddf294ef15c80bb64a2cdcf15d5b25caf88fb4f4a12383bc9f7a01a09c2e3"
FIM_MODEL_SIZE = 415_182_720
FIM_LICENSE_URL = f"https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/{FIM_MODEL_REVISION}/LICENSE"
FIM_LICENSE_SHA256 = "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e"
RELEASE_ASSET_NAMES = frozenset({"manifest.json", CUDA_CERTIFICATION_ASSET, CONSUMER_AUTHORITY_ASSET, *EXPECTED_LEGS})
REBUILD_INPUT_FIELDS = frozenset({
    "source_tree",
    "cargo_lock_sha256",
    "workspace_manifest_sha256",
    "engine_manifest_sha256",
    "engine_build_sha256",
    "vendor_tree_sha256",
    "vendor_license_sha256",
    "converter_requirements_sha256",
    "native_fixture_model_url",
    "native_fixture_model_sha256",
    "native_fixture_license_url",
    "native_fixture_license_sha256",
    "fim_fixture_model_url",
    "fim_fixture_model_revision",
    "fim_fixture_model_sha256",
    "fim_fixture_model_size_bytes",
    "fim_fixture_license_url",
    "fim_fixture_license_sha256",
})
PUBLISH_JOB_CONDITION = (
    "github.event_name == 'workflow_dispatch' && inputs.publish_release && startsWith(github.ref, 'refs/tags/v')"
)
MACOS_PILOT_CONDITION = "github.event_name == 'workflow_dispatch' && inputs.macos_pilot && !inputs.publish_release"
PUBLISH_ONLY_JOBS = frozenset({
    "cuda-certification",
    "cuda-certification-binding",
    "publication-prerequisites",
    "sign-windows",
    "validate-signed-windows",
    "assemble-release",
    "publish",
    "verify-release",
    "verify-release-windows",
})
CI_VERIFIER_REQUIREMENTS = frozenset({
    "execnet",
    "httpx",
    "jsonschema",
    "license-expression",
    "pytest",
    "pytest-asyncio",
    "pytest-randomly",
    "pytest-timeout",
    "pytest-xdist",
    "pyyaml",
    "sigstore",
    "uv",
})


class ReleaseValidationError(ValueError):
    """Raised when release evidence is incomplete, malformed, or inconsistent."""


@dataclass(frozen=True)
class SizeRecord:
    """One committed server-binary size measurement and approved ceiling."""

    baseline_bytes: int | None
    ceiling_bytes: int | None
    workflow_run_url: str | None


@dataclass(frozen=True)
class ReleaseIdentity:
    """Identity fields that every fragment and merged manifest must bind."""

    repository: str
    source_commit: str
    source_ref: str
    workflow: str
    run_id: str

    def as_dict(self) -> dict[str, str]:
        """Return the manifest representation of the release identity.

        Returns:
            A new mapping suitable for exact manifest comparison.
        """
        return {
            "repository": self.repository,
            "source_commit": self.source_commit,
            "source_ref": self.source_ref,
            "workflow": self.workflow,
            "run_id": self.run_id,
        }


def sha256_file(path: Path) -> str:
    """Calculate a file SHA-256 without loading the whole file into memory.

    Args:
        path: File to hash.

    Returns:
        Lowercase hexadecimal SHA-256 digest.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_size_ledger(path: Path, *, require_measured: bool = False) -> dict[str, SizeRecord]:
    """Load and validate the committed per-leg size ledger.

    Args:
        path: JSON ledger path.
        require_measured: Require every baseline, ceiling, and workflow URL.

    Returns:
        Size records keyed by matrix bundle identifier.

    Raises:
        ReleaseValidationError: If the ledger is malformed or unmeasured when required.
    """
    payload = _read_json_object(path, "size baseline ledger")
    if set(payload) != {"schema_version", "ceiling_formula", "artifacts"} or payload["schema_version"] != 1:
        raise ReleaseValidationError("size baseline ledger fields or schema version are invalid")
    if payload["ceiling_formula"] != "ceil(baseline_bytes * 1.20 / 1048576) * 1048576":
        raise ReleaseValidationError("size baseline ledger ceiling formula is not canonical")
    artifacts = payload["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != set(SIZE_LEDGER_KEYS):
        raise ReleaseValidationError("size baseline ledger must contain exactly the four engine matrix legs")
    records: dict[str, SizeRecord] = {}
    for bundle, raw in artifacts.items():
        if not isinstance(raw, dict) or set(raw) != {"baseline_bytes", "ceiling_bytes", "workflow_run_url"}:
            raise ReleaseValidationError(f"size baseline fields are invalid for {bundle}")
        baseline = _optional_positive_int(raw["baseline_bytes"], f"{bundle} baseline_bytes")
        ceiling = _optional_positive_int(raw["ceiling_bytes"], f"{bundle} ceiling_bytes")
        url = raw["workflow_run_url"]
        if url is not None and (
            not isinstance(url, str) or not re.fullmatch(r"https://github\.com/.+/actions/runs/\d+", url)
        ):
            raise ReleaseValidationError(f"{bundle} workflow_run_url is not an immutable Actions run URL")
        if (baseline is None) != (ceiling is None) or (baseline is None) != (url is None):
            raise ReleaseValidationError(f"{bundle} size evidence must be wholly measured or wholly absent")
        if baseline is not None:
            expected_ceiling = ((baseline * 120 + 99) // 100 + 1_048_575) // 1_048_576 * 1_048_576
            if ceiling != expected_ceiling:
                raise ReleaseValidationError(f"{bundle} ceiling does not match the canonical measured-baseline formula")
        if require_measured and baseline is None:
            raise ReleaseValidationError(f"{bundle} has no governed size measurement; publication is blocked")
        records[bundle] = SizeRecord(baseline, ceiling, url)
    return records


def validate_workflow_size_bindings(workflow_path: Path) -> None:
    """Verify workflow matrix legs consume the committed ledger instead of hidden variables.

    Args:
        workflow_path: Engine workflow YAML path.

    Raises:
        ReleaseValidationError: If a leg is absent, misbound, duplicated, or uses repository size variables.
    """
    text = workflow_path.read_text(encoding="utf-8")
    if "vars.ENGINE_SIZE_" in text or "ENGINE_SIZE_CEILING" in text:
        raise ReleaseValidationError("engine workflow must not use uncommitted repository size variables")
    bindings = re.findall(
        r"- bundle: ([a-z-]+)\n(?: {12}.+\n)*? {12}size_ledger_key: ([a-z-]+)\n",
        text,
    )
    if sorted(bindings) != sorted((key, key) for key in SIZE_LEDGER_KEYS):
        raise ReleaseValidationError("engine workflow matrix is not bound exactly to all committed size-ledger keys")
    required_call = "scripts/validate_engine_release.py size"
    if required_call not in text or "docs/reference/engine-size-baselines.json" not in text:
        raise ReleaseValidationError("engine workflow does not execute the committed size-ledger validator")
    enforce_step = re.search(
        r"      - name: Enforce binary size budget\n(?P<body>.*?)(?=\n      - name:)",
        text,
        re.DOTALL,
    )
    if enforce_step is None:
        raise ReleaseValidationError("engine workflow has no release size-enforcement step")
    body = enforce_step.group("body")
    expected_body = "\n".join((
        (
            "        if: github.event_name == 'workflow_dispatch' && inputs.publish_release "
            "&& startsWith(github.ref, 'refs/tags/v')"
        ),
        "        shell: bash",
        "        run: |",
        "          python scripts/validate_engine_release.py size \\",
        '            --bundle "${{ matrix.size_ledger_key }}" \\',
        '            --binary "$BUNDLE_DIR/amw-engine-server${{ matrix.executable_suffix }}" \\',
        "            --ledger docs/reference/engine-size-baselines.json \\",
        "            --enforce",
    ))
    if body != expected_body:
        raise ReleaseValidationError(
            "engine release size step is not publish-gated and fail-closed on the committed ledger"
        )
    sign_job = re.search(
        r"  sign-windows:\n(?P<body>.*?)(?=\n  [a-zA-Z0-9_-]+:)",
        text,
        re.DOTALL,
    )
    signed_size_markers = (
        "          name: engine-release-policy\n",
        "          path: release-policy\n",
        "      - name: Sign Windows binaries with Azure Trusted Signing\n",
        "      - name: Enforce signed server size budget before bundle rebuild\n",
        "          & python release-policy/validate_engine_release.py size `\n",
        '            --bundle "${{ matrix.bundle }}" `\n',
        '            --binary "signing-input/amw-engine-server.exe" `\n',
        "            --ledger release-policy/engine-size-baselines.json `\n",
        "            --enforce\n",
        "      - name: Verify signatures and rebuild signed bundle\n",
    )
    if sign_job is None or any(marker not in sign_job.group("body") for marker in signed_size_markers):
        raise ReleaseValidationError("signed Windows server size is not enforced against the committed ledger")
    sign_body = sign_job.group("body")
    signing_index = sign_body.index("      - name: Sign Windows binaries with Azure Trusted Signing\n")
    signed_size_index = sign_body.index("      - name: Enforce signed server size budget before bundle rebuild\n")
    rebuild_index = sign_body.index("      - name: Verify signatures and rebuild signed bundle\n")
    if not signing_index < signed_size_index < rebuild_index:
        raise ReleaseValidationError("signed Windows server size is not enforced before archive rebuild")


_PUBLISH_JOB_CONDITION = (
    "github.event_name == 'workflow_dispatch' && inputs.publish_release && startsWith(github.ref, 'refs/tags/v')"
)
_APPROVED_EXPORT_STEP_SHA256 = {
    ("build", "Build llama export tools"): "8a101d7edd0c80e85f784061375e180f379e7448bf1a2bbf4ab35a01aee8c671",
    (
        "sign-windows",
        "Verify signatures and rebuild signed bundle",
    ): "ce78649ecd64bbf9392eadd6b779b7a8dfde48854f9a7319acc505a3472b0066",
    (
        "validate-signed-windows",
        "Provision uncached converter validation environment",
    ): "45145be422383a5d6eea4ed014006faf7cc863554cb77fffa735fd14b55d0241",
    (
        "validate-signed-windows",
        "Verify downloaded signatures and exercise release-bootstrap export toolchain",
    ): "5dedd154fd82022d7b4d9513ba5825b90456ab4eb9c85996d2023e97b0783a4f",
    (
        "assemble-release",
        "Validate signed Windows export bootstrap receipts",
    ): "80f7e34867a99b66164b6e21098352a525f65b79373ebbc247c839d778614cbb",
}


def _workflow_jobs(workflow_path: Path) -> dict[str, dict[str, Any]]:
    """Read the workflow surfaces used by release policy with no YAML package.

    This is deliberately a narrow indentation-aware reader, not a general YAML
    implementation.  It recognizes the GitHub Actions job/step vocabulary that
    this validator authorizes and fails closed on malformed relevant structure.
    Keeping it in this single publisher handoff removes any dependency on the
    mutable runner-global Python environment.
    """
    try:
        lines = workflow_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseValidationError("engine workflow is not readable structured YAML") from exc
    jobs_indexes = [index for index, line in enumerate(lines) if re.fullmatch(r"jobs:\s*(?:#.*)?", line)]
    if len(jobs_indexes) != 1:
        raise ReleaseValidationError("engine workflow has no structured jobs mapping")
    jobs: dict[str, dict[str, Any]] = {}
    index = jobs_indexes[0] + 1
    while index < len(lines):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) < 2:
            break
        match = re.fullmatch(r"  ([A-Za-z0-9_-]+):\s*(?:#.*)?", line)
        if match is None:
            index += 1
            continue
        job_name = match.group(1)
        if job_name in jobs:
            raise ReleaseValidationError(f"engine workflow repeats job {job_name!r}")
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= 2:
                break
            end += 1
        jobs[job_name] = _parse_workflow_job(lines, index + 1, end, job_name=job_name)
        index = end
    if not jobs:
        raise ReleaseValidationError("engine workflow has no structured jobs mapping")
    return jobs


def _yaml_scalar(value: str) -> object:
    """Decode the small scalar/list subset used by governed workflow fields."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1].replace("''", "'") if stripped[0] == "'" else json.loads(stripped)
    if stripped.startswith("[") and stripped.endswith("]"):
        body = stripped[1:-1].strip()
        return [] if not body else [_yaml_scalar(item) for item in body.split(",")]
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    return stripped


def _block_scalar(lines: list[str], start: int, end: int, *, parent_indent: int) -> tuple[str, int]:
    """Return a literal block scalar and the first unconsumed line index."""
    cursor = start
    captured: list[str] = []
    content_indent: int | None = None
    while cursor < end:
        line = lines[cursor]
        indent = len(line) - len(line.lstrip()) if line.strip() else parent_indent + 2
        if line.strip() and indent <= parent_indent:
            break
        if line.strip() and content_indent is None:
            content_indent = indent
        captured.append(line)
        cursor += 1
    trim = content_indent or parent_indent + 2
    return "\n".join(line[trim:] if len(line) >= trim else "" for line in captured), cursor


def _parse_step_mapping(
    lines: list[str],
    start: int,
    end: int,
    *,
    base_indent: int,
) -> tuple[dict[str, object], int]:
    """Parse one governed step or nested step mapping."""
    row: dict[str, object] = {}
    cursor = start
    while cursor < end:
        line = lines[cursor]
        if not line.strip() or line.lstrip().startswith("#"):
            cursor += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent < base_indent:
            break
        if indent != base_indent:
            raise ReleaseValidationError("engine workflow has unsupported governed step indentation")
        match = re.fullmatch(rf"\s{{{base_indent}}}([A-Za-z0-9_-]+):(?:\s*(.*))?", line)
        if match is None:
            raise ReleaseValidationError("engine workflow has malformed governed step fields")
        key, raw_value = match.group(1), (match.group(2) or "")
        if key in row:
            raise ReleaseValidationError(f"engine workflow repeats governed step field {key!r}")
        if raw_value in {"|", "|-", "|+", ">", ">-", ">+"}:
            value, cursor = _block_scalar(lines, cursor + 1, end, parent_indent=base_indent)
            row[key] = value
            continue
        if raw_value:
            row[key] = _yaml_scalar(raw_value)
            cursor += 1
            continue
        nested, cursor = _parse_step_mapping(lines, cursor + 1, end, base_indent=base_indent + 2)
        row[key] = nested
    return row, cursor


def _parse_workflow_steps(lines: list[str], start: int, end: int) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    cursor = start
    while cursor < end:
        line = lines[cursor]
        if not line.strip() or line.lstrip().startswith("#"):
            cursor += 1
            continue
        match = re.fullmatch(r"      -\s+([A-Za-z0-9_-]+):(?:\s*(.*))?", line)
        if match is None:
            break
        key, raw_value = match.group(1), (match.group(2) or "")
        step: dict[str, object] = {key: _yaml_scalar(raw_value)} if raw_value else {}
        cursor += 1
        continuation, cursor = _parse_step_mapping(lines, cursor, end, base_indent=8)
        overlap = set(step) & set(continuation)
        if overlap:
            raise ReleaseValidationError(f"engine workflow repeats governed step fields: {sorted(overlap)}")
        step.update(continuation)
        steps.append(step)
    return steps


def _parse_workflow_job(lines: list[str], start: int, end: int, *, job_name: str) -> dict[str, Any]:
    job: dict[str, Any] = {}
    cursor = start
    while cursor < end:
        line = lines[cursor]
        if not line.strip() or line.lstrip().startswith("#"):
            cursor += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent != 4:
            cursor += 1
            continue
        match = re.fullmatch(r"    ([A-Za-z0-9_-]+):(?:\s*(.*))?", line)
        if match is None:
            raise ReleaseValidationError(f"engine workflow job {job_name!r} has malformed fields")
        key, raw_value = match.group(1), (match.group(2) or "")
        if key in job:
            raise ReleaseValidationError(f"engine workflow job {job_name!r} repeats field {key!r}")
        if key == "steps":
            job[key] = _parse_workflow_steps(lines, cursor + 1, end)
        elif raw_value:
            job[key] = _yaml_scalar(raw_value)
        cursor += 1
    return job


def _workflow_steps(job: dict[str, Any], *, job_name: str) -> list[dict[str, Any]]:
    steps = job.get("steps")
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise ReleaseValidationError(f"engine workflow job {job_name!r} has no structured steps")
    return steps


def _named_workflow_step(job: dict[str, Any], *, job_name: str, step_name: str) -> tuple[int, dict[str, Any]]:
    matches = [
        (index, step)
        for index, step in enumerate(_workflow_steps(job, job_name=job_name))
        if step.get("name") == step_name
    ]
    if len(matches) != 1:
        raise ReleaseValidationError(f"engine workflow job {job_name!r} must own exactly one {step_name!r} step")
    return matches[0]


def _active_step_script(job: dict[str, Any], *, job_name: str, step_name: str) -> str:
    _index, step = _named_workflow_step(job, job_name=job_name, step_name=step_name)
    script = step.get("run")
    if not isinstance(script, str) or not script.strip():
        raise ReleaseValidationError(f"engine workflow step {job_name!r}/{step_name!r} is not executable")
    active_lines = [_strip_shell_comment(line) for line in script.splitlines()]
    return "\n".join(line for line in active_lines if line.strip() and not line.lstrip().startswith("//"))


def _strip_shell_comment(line: str) -> str:
    """Remove an active shell comment while preserving hashes inside strings."""
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character in {"`", "\\"}:
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
            continue
        if character == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line


def _require_script_patterns(
    job: dict[str, Any],
    *,
    job_name: str,
    step_name: str,
    patterns: tuple[str, ...],
    reachable_patterns: tuple[tuple[str, str, tuple[str, ...]], ...] = (),
) -> None:
    script = _active_step_script(job, job_name=job_name, step_name=step_name)
    normalized = re.sub(r"\s+", " ", script)
    missing = [pattern for pattern in patterns if re.search(pattern, normalized) is None]
    missing_reachable = [
        pattern
        for pattern, shell_name, allowed_ancestors in reachable_patterns
        if not _has_reachable_line(
            script,
            pattern=pattern,
            shell_name=shell_name,
            allowed_ancestors=allowed_ancestors,
        )
    ]
    if missing or missing_reachable:
        raise ReleaseValidationError(
            f"engine workflow executable step {job_name!r}/{step_name!r} is incomplete: {missing + missing_reachable}"
        )


def _require_approved_step_body(job: dict[str, Any], *, job_name: str, step_name: str) -> None:
    """Require the complete active body approved for a security-critical step."""
    expected = _APPROVED_EXPORT_STEP_SHA256[job_name, step_name]
    script = _active_step_script(job, job_name=job_name, step_name=step_name)
    observed = hashlib.sha256(script.encode("utf-8")).hexdigest()
    if observed != expected:
        raise ReleaseValidationError(
            f"engine workflow executable step {job_name!r}/{step_name!r} differs from its approved whole-step contract"
        )


def _has_reachable_line(
    script: str,
    *,
    pattern: str,
    shell_name: str,
    allowed_ancestors: tuple[str, ...],
) -> bool:
    """Return whether a required command appears in its approved control context."""
    stack: list[str] = []
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if shell_name == "bash":
            if line == "fi":
                if not stack or not stack[-1].startswith("if"):
                    return False
                stack.pop()
                continue
            if line == "done":
                if not stack or stack[-1] != "loop":
                    return False
                stack.pop()
                continue
            if line == "esac":
                if not stack or stack[-1] != "case":
                    return False
                stack.pop()
                continue
            if line == "}":
                if not stack or stack[-1] != "block":
                    return False
                stack.pop()
                continue
            if re.fullmatch(pattern, line) is not None and tuple(stack) == allowed_ancestors:
                return True
            if_match = re.fullmatch(r"if\s+(.+?)(?:;\s*then)?", line)
            if if_match is not None:
                condition = re.sub(r"\s+", " ", if_match.group(1).strip())
                stack.append("if-false" if condition in {"false", "! true"} else "if")
            elif re.fullmatch(r"(?:for|while|until)\b.*", line):
                stack.append("loop")
            elif re.fullmatch(r"case\b.*\bin", line):
                stack.append("case")
            elif re.fullmatch(r"(?:function\s+)?[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\)\s*\{", line):
                stack.append("block")
            continue
        if shell_name != "pwsh":
            raise ReleaseValidationError(f"unsupported governed workflow shell: {shell_name}")
        while line.startswith("}"):
            if not stack:
                return False
            stack.pop()
            line = line[1:].lstrip()
        if re.fullmatch(pattern, line) is not None and tuple(stack) == allowed_ancestors:
            return True
        opens = line.count("{") - line.count("}")
        if opens <= 0:
            continue
        if re.match(r"foreach\s*\(", line, re.IGNORECASE):
            kind = "foreach"
        elif re.match(r"if\s*\(\s*\$false\s*\)", line, re.IGNORECASE):
            kind = "if-false"
        elif re.match(r"if\s*\(", line, re.IGNORECASE):
            kind = "if"
        else:
            kind = "block"
        stack.extend([kind] * opens)
    return False


def _needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    return set()


def _require_publish_job(job: dict[str, Any], *, job_name: str, needs: set[str]) -> None:
    if job.get("if") != _PUBLISH_JOB_CONDITION or not needs.issubset(_needs(job)):
        raise ReleaseValidationError(f"engine workflow publish job {job_name!r} has unsafe condition or dependencies")


def validate_workflow_export_toolchain_bindings(workflow_path: Path) -> None:
    """Structurally require build, signing, bootstrap, and assembly ownership."""
    jobs = _workflow_jobs(workflow_path)
    required_jobs = {
        "build",
        "cuda-certification",
        "cuda-certification-binding",
        "publication-prerequisites",
        "sign-windows",
        "validate-signed-windows",
        "assemble-release",
        "publish",
        "verify-release",
    }
    if not required_jobs.issubset(jobs):
        raise ReleaseValidationError(
            f"engine workflow is missing export-toolchain jobs: {sorted(required_jobs - jobs.keys())}"
        )
    build = jobs["build"]
    cuda_certification = jobs["cuda-certification"]
    cuda_binding = jobs["cuda-certification-binding"]
    publication_prerequisites = jobs["publication-prerequisites"]
    sign = jobs["sign-windows"]
    validate = jobs["validate-signed-windows"]
    assemble = jobs["assemble-release"]
    publish = jobs["publish"]
    verify_release = jobs["verify-release"]
    if "supply-chain" not in _needs(build):
        raise ReleaseValidationError("engine workflow build job does not depend on supply-chain validation")
    _require_publish_job(cuda_certification, job_name="cuda-certification", needs={"build"})
    _require_publish_job(
        cuda_binding,
        job_name="cuda-certification-binding",
        needs={"cuda-certification"},
    )
    _require_publish_job(
        publication_prerequisites,
        job_name="publication-prerequisites",
        needs={"supply-chain", "build", "cuda-certification-binding"},
    )
    _require_publish_job(sign, job_name="sign-windows", needs={"build", "publication-prerequisites"})
    _require_publish_job(validate, job_name="validate-signed-windows", needs={"sign-windows"})
    _require_publish_job(
        assemble,
        job_name="assemble-release",
        needs={"build", "cuda-certification-binding", "sign-windows", "validate-signed-windows"},
    )
    _require_publish_job(publish, job_name="publish", needs={"assemble-release"})
    _require_publish_job(verify_release, job_name="verify-release", needs={"publish"})

    _require_script_patterns(
        build,
        job_name="build",
        step_name="Build llama export tools",
        patterns=(r"cmake -S crates/amw-engine/vendor/llama\.cpp -B build/llama",),
        reachable_patterns=(
            (
                r"cmake --build build/llama --config Release --target llama-quantize llama-imatrix --parallel 2",
                "bash",
                (),
            ),
        ),
    )
    _require_script_patterns(
        build,
        job_name="build",
        step_name="Stage required bundle content",
        patterns=(
            r"expected one llama-imatrix binary",
            r'shutil\.copy2\(vendor / "convert_hf_to_gguf\.py"',
            r'shutil\.copy2\(vendor / "convert_lora_to_gguf\.py"',
        ),
    )
    _require_script_patterns(
        build,
        job_name="build",
        step_name="Generate inner manifest and deterministic bundle",
        patterns=(
            r'"llama-imatrix\$\{\{ matrix\.executable_suffix \}\}"',
            r'"convert_hf_to_gguf\.py"',
            r'relative in \{"amw-engine-server", "llama-imatrix", "llama-quantize"\}',
        ),
    )

    _require_script_patterns(
        sign,
        job_name="sign-windows",
        step_name="Verify signatures and rebuild signed bundle",
        patterns=(),
        reachable_patterns=(
            (r"if \(\$executables\.Count -ne 3\) \{.*\}", "pwsh", ()),
            (
                r"if \(\$signature\.SignerCertificate\.Subject -cne \$env:WINDOWS_SIGNER_SUBJECT\) \{",
                "pwsh",
                ("foreach",),
            ),
            (
                r"if \(\$signature\.SignerCertificate\.Issuer -cne \$env:WINDOWS_SIGNER_ISSUER\) \{",
                "pwsh",
                ("foreach",),
            ),
            (
                r"if \(-not \(\$signature\.SignerCertificate\.EnhancedKeyUsageList\.ObjectId\.Value -contains \$codeSigningOid\)\) \{",
                "pwsh",
                ("foreach",),
            ),
        ),
    )
    provision_index, provision_step = _named_workflow_step(
        validate,
        job_name="validate-signed-windows",
        step_name="Provision uncached converter validation environment",
    )
    closure_authority_index, closure_authority_step = _named_workflow_step(
        validate,
        job_name="validate-signed-windows",
        step_name="Protect signed-validator interpreter closure",
    )
    bootstrap_index, bootstrap_step = _named_workflow_step(
        validate,
        job_name="validate-signed-windows",
        step_name="Verify downloaded signatures and exercise release-bootstrap export toolchain",
    )
    if closure_authority_index != provision_index + 1 or bootstrap_index != closure_authority_index + 1:
        raise ReleaseValidationError(
            "engine workflow release-bootstrap probe does not immediately follow protected closure publication"
        )
    if closure_authority_step != {
        "name": "Protect signed-validator interpreter closure",
        "uses": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "with": {
            "name": "protected-signed-validator-interpreter-closure-${{ matrix.bundle }}",
            "path": (
                "signed-release/signed-validator-interpreter-closure-${{ matrix.bundle }}.json\n"
                "signed-release/signed-validator-interpreter-closure-${{ matrix.bundle }}.objects.zip"
            ),
            "if-no-files-found": "error",
        },
    }:
        raise ReleaseValidationError("engine workflow protected interpreter closure publication is not exact")
    expected_provision_env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if provision_step.get("env") != expected_provision_env:
        raise ReleaseValidationError("engine workflow converter provisioning environment is not exact")
    expected_env = {
        "WINDOWS_SIGNER_SUBJECT": "${{ vars.ENGINE_WINDOWS_SIGNER_SUBJECT }}",
        "WINDOWS_SIGNER_ISSUER": "${{ vars.ENGINE_WINDOWS_SIGNER_ISSUER }}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if bootstrap_step.get("env") != expected_env:
        raise ReleaseValidationError("engine workflow release-bootstrap signer authority is not exact")
    _require_script_patterns(
        validate,
        job_name="validate-signed-windows",
        step_name="Provision uncached converter validation environment",
        patterns=(
            r"scripts/validate_engine_release\.py archive",
            r"--run-smokes",
            r"--prepare-fixture \$fixtureRoot",
            r"--write-interpreter-closure",
            r"--write-interpreter-closure-objects",
            r"signed-validator-interpreter-closure-\$\{\{ matrix\.bundle \}\}\.json",
            r"signed-validator-interpreter-closure-\$\{\{ matrix\.bundle \}\}\.objects\.zip",
        ),
    )
    _require_script_patterns(
        validate,
        job_name="validate-signed-windows",
        step_name="Verify downloaded signatures and exercise release-bootstrap export toolchain",
        patterns=(
            r"Get-FileHash -Algorithm SHA256",
            r"--bootstrap-bundle-root \$bootstrapRoot",
            r"--bootstrap-platform windows",
            r'--bootstrap-accelerator "\$\{\{ matrix\.accel \}\}"',
            r"--bootstrap-inner-manifest-sha256 \$innerManifestSha256",
            r'--bootstrap-source-commit "\$\{\{ github\.sha \}\}"',
            r'--expected-interpreter-closure "signed-release/signed-validator-interpreter-closure-\$\{\{ matrix\.bundle \}\}\.json"',
            r'--expected-interpreter-closure-objects "signed-release/signed-validator-interpreter-closure-\$\{\{ matrix\.bundle \}\}\.objects\.zip"',
        ),
        reachable_patterns=(
            (r"if \(\$executables\.Count -ne 3\) \{.*\}", "pwsh", ()),
            (
                r"if \(\$signature\.SignerCertificate\.Subject -cne \$env:WINDOWS_SIGNER_SUBJECT\) \{",
                "pwsh",
                ("foreach",),
            ),
            (
                r"if \(\$signature\.SignerCertificate\.Issuer -cne \$env:WINDOWS_SIGNER_ISSUER\) \{",
                "pwsh",
                ("foreach",),
            ),
            (
                r'if \(-not \(\$signature\.SignerCertificate\.EnhancedKeyUsageList\.ObjectId\.Value -contains "1\.3\.6\.1\.5\.5\.7\.3\.3"\)\) \{',
                "pwsh",
                ("foreach",),
            ),
            (r"python scripts/probe_engine_export_toolchain\.py `", "pwsh", ()),
        ),
    )
    upload_steps = [
        step
        for step in _workflow_steps(validate, job_name="validate-signed-windows")
        if isinstance(step.get("uses"), str) and step["uses"].startswith("actions/upload-artifact@")
    ]
    expected_uploads = [
        {
            "name": "protected-signed-validator-interpreter-closure-${{ matrix.bundle }}",
            "path": (
                "signed-release/signed-validator-interpreter-closure-${{ matrix.bundle }}.json\n"
                "signed-release/signed-validator-interpreter-closure-${{ matrix.bundle }}.objects.zip"
            ),
            "if-no-files-found": "error",
        },
        {
            "name": "validated-release-bootstrap-export-toolchain-${{ matrix.bundle }}",
            "path": (
                "signed-release/release-bootstrap-export-toolchain-${{ matrix.bundle }}.json\n"
                "signed-release/release-bootstrap-export-toolchain-${{ matrix.bundle }}.evidence.zip"
            ),
            "if-no-files-found": "error",
        },
    ]
    if len(upload_steps) != 2 or [step.get("with") for step in upload_steps] != expected_uploads:
        raise ReleaseValidationError("engine workflow closure authority and release-bootstrap uploads are not exact")

    assemble_downloads = [
        step
        for step in _workflow_steps(assemble, job_name="assemble-release")
        if isinstance(step.get("uses"), str) and step["uses"].startswith("actions/download-artifact@")
    ]
    expected_receipt_downloads = {
        ("validated-release-bootstrap-export-toolchain-windows-cpu", "release-bootstrap-receipts/windows-cpu"),
        ("validated-release-bootstrap-export-toolchain-windows-cuda", "release-bootstrap-receipts/windows-cuda"),
    }
    observed_receipt_downloads = {
        (str(step.get("with", {}).get("name")), str(step.get("with", {}).get("path")))
        for step in assemble_downloads
        if isinstance(step.get("with"), dict)
        and str(step["with"].get("name", "")).startswith("validated-release-bootstrap-")
    }
    if observed_receipt_downloads != expected_receipt_downloads:
        raise ReleaseValidationError("assemble-release does not download the exact bootstrap receipt artifacts")
    expected_closure_downloads = {
        (
            "protected-signed-validator-interpreter-closure-windows-cpu",
            "release-bootstrap-interpreter-closures/windows-cpu",
        ),
        (
            "protected-signed-validator-interpreter-closure-windows-cuda",
            "release-bootstrap-interpreter-closures/windows-cuda",
        ),
    }
    observed_closure_downloads = {
        (str(step.get("with", {}).get("name")), str(step.get("with", {}).get("path")))
        for step in assemble_downloads
        if isinstance(step.get("with"), dict)
        and str(step["with"].get("name", "")).startswith("protected-signed-validator-interpreter-closure-")
    }
    if observed_closure_downloads != expected_closure_downloads:
        raise ReleaseValidationError("assemble-release does not download the exact protected closure authorities")
    receipt_index, _receipt_step = _named_workflow_step(
        assemble,
        job_name="assemble-release",
        step_name="Validate signed Windows export bootstrap receipts",
    )
    _require_script_patterns(
        assemble,
        job_name="assemble-release",
        step_name="Validate signed Windows export bootstrap receipts",
        patterns=(
            r"--release-root release-assets",
            r"--receipts-root release-bootstrap-receipts",
            r"--closures-root release-bootstrap-interpreter-closures",
            r'--expected-commit "\$\{\{ github\.sha \}\}"',
        ),
        reachable_patterns=((r"python scripts/validate_engine_release\.py bootstrap-receipts \\", "bash", ()),),
    )
    merge_index, _merge_step = _named_workflow_step(
        assemble,
        job_name="assemble-release",
        step_name="Merge and validate every provenance leg",
    )
    attest_index, attest_step = _named_workflow_step(
        assemble,
        job_name="assemble-release",
        step_name="Generate identity-bound Sigstore build provenance",
    )
    handoff_index, _handoff_step = _named_workflow_step(
        assemble,
        job_name="assemble-release",
        step_name="Stage flat publisher handoff",
    )
    if not receipt_index < merge_index < attest_index < handoff_index:
        raise ReleaseValidationError(
            "bootstrap receipts and consumer release authority are not validated, merged, attested, and staged in order"
        )
    subject_paths = (
        attest_step.get("with", {}).get("subject-path") if isinstance(attest_step.get("with"), dict) else None
    )
    expected_subjects = {
        "release-assets/manifest.json",
        "release-assets/consumer-release-authority.json",
        "release-assets/cuda-certification.json",
        "release-assets/amw-engine-*.zip",
    }
    if (
        not isinstance(attest_step.get("uses"), str)
        or not attest_step["uses"].startswith("actions/attest@")
        or not isinstance(subject_paths, str)
        or set(subject_paths.splitlines()) != expected_subjects
    ):
        raise ReleaseValidationError("consumer release authority is not in the exact Sigstore subject set")
    _require_script_patterns(
        assemble,
        job_name="assemble-release",
        step_name="Merge and validate every provenance leg",
        patterns=(
            r"scripts/validate_engine_release\.py merge",
            r"--output release-assets/manifest\.json",
        ),
    )
    _require_script_patterns(
        assemble,
        job_name="assemble-release",
        step_name="Stage flat publisher handoff",
        patterns=(
            r'source / "consumer-release-authority\.json"',
            r'"consumer-release-authority\.json"',
        ),
    )
    for job, job_name, step_name in (
        (build, "build", "Build llama export tools"),
        (sign, "sign-windows", "Verify signatures and rebuild signed bundle"),
        (
            validate,
            "validate-signed-windows",
            "Provision uncached converter validation environment",
        ),
        (
            validate,
            "validate-signed-windows",
            "Verify downloaded signatures and exercise release-bootstrap export toolchain",
        ),
        (assemble, "assemble-release", "Validate signed Windows export bootstrap receipts"),
    ):
        _require_approved_step_body(job, job_name=job_name, step_name=step_name)


def validate_binary_size(*, bundle: str, binary: Path, ledger_path: Path, enforce: bool) -> dict[str, int | None]:
    """Measure a server binary and optionally enforce its governed ceiling.

    Args:
        bundle: Matrix bundle identifier.
        binary: Built server executable.
        ledger_path: Committed size ledger.
        enforce: Require measured evidence and reject ceiling overflow.

    Returns:
        Actual size and the governed or proposed ceiling.

    Raises:
        ReleaseValidationError: If the artifact or ledger is invalid or the budget is exceeded.
    """
    records = load_size_ledger(ledger_path, require_measured=enforce)
    if bundle not in records:
        raise ReleaseValidationError(f"unknown size-ledger bundle: {bundle}")
    if not binary.is_file():
        raise ReleaseValidationError(f"server binary is missing: {binary}")
    actual = binary.stat().st_size
    record = records[bundle]
    proposed = ((actual * 120 + 99) // 100 + 1_048_575) // 1_048_576 * 1_048_576
    if enforce and record.ceiling_bytes is not None and actual > record.ceiling_bytes:
        raise ReleaseValidationError(
            f"{bundle} server size {actual} exceeds governed ceiling {record.ceiling_bytes} bytes"
        )
    return {"actual_bytes": actual, "ceiling_bytes": record.ceiling_bytes, "proposed_ceiling_bytes": proposed}


def validate_archive(
    archive_path: Path,
    *,
    platform: str,
    accelerator: str,
    converter_python: Path | None = None,
    run_smokes: bool = False,
) -> dict[str, Any]:
    """Validate one archive's safety, inner manifest, SPDX identity, and packaged tools.

    Args:
        archive_path: Engine ZIP archive.
        platform: Expected platform selector.
        accelerator: Expected accelerator selector.
        converter_python: Absolute interpreter with locked converter dependencies.
        run_smokes: Execute the packaged server, quantizer, and converter help paths.

    Returns:
        Parsed inner manifest.

    Raises:
        ReleaseValidationError: If archive safety, content, identity, digest, or smoke checks fail.
    """
    expected_name = f"amw-engine-{platform}-{accelerator}.zip"
    if archive_path.name != expected_name or expected_name not in EXPECTED_LEGS:
        raise ReleaseValidationError(f"archive selector does not match expected filename {expected_name}")
    with tempfile.TemporaryDirectory(prefix="amw-engine-release-") as temporary:
        extracted = Path(temporary)
        _safe_extract_zip(archive_path, extracted)
        inner = _read_json_object(extracted / "manifest.json", "inner release manifest")
        if set(inner) != {"engine_version", "libllama_rev", "min_pkg_version", "artifacts"}:
            raise ReleaseValidationError("inner release manifest fields are invalid")
        rows = inner["artifacts"]
        if not isinstance(rows, list) or not rows:
            raise ReleaseValidationError("inner release manifest has no artifact rows")
        expected_files = {
            path.relative_to(extracted).as_posix()
            for path in extracted.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }
        suffix = ".exe" if platform == "windows" else ""
        required_files = {
            f"amw-engine-server{suffix}",
            "requirements-convert_lora_to_gguf.txt",
            "LICENSE.llama.cpp",
            "NOTICE",
            "ENGINE_THIRD_PARTY_LICENSES.md",
            LICENSE_INDEX_MEMBER,
            SPDX_MEMBER,
            "conversion/__init__.py",
            "conversion/base.py",
            "gguf-py/gguf/__init__.py",
            "gguf-py/gguf/py.typed",
        } | set(export_tool_members(platform=platform))
        missing_required = required_files - expected_files
        if missing_required:
            raise ReleaseValidationError(f"engine archive is missing required members: {sorted(missing_required)}")
        observed_files: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"platform", "accel", "file", "sha256", "size_bytes"}:
                raise ReleaseValidationError("inner release manifest artifact fields are invalid")
            name = row["file"]
            if not isinstance(name, str) or name in observed_files:
                raise ReleaseValidationError("inner release manifest contains a duplicate or invalid filename")
            if (row["platform"], row["accel"]) != (platform, accelerator):
                raise ReleaseValidationError(f"inner selector mismatch for {name}")
            path = extracted / PurePosixPath(name)
            if not path.is_file() or path.stat().st_size != row["size_bytes"] or sha256_file(path) != row["sha256"]:
                raise ReleaseValidationError(f"inner digest or size mismatch for {name}")
            observed_files.add(name)
        if observed_files != expected_files:
            raise ReleaseValidationError("inner release manifest does not cover the exact extracted payload")
        native_members = (
            {f"amw-engine-server{suffix}"}
            | {export_tool_member(tool, platform=platform) for tool in EXPORT_NATIVE_TOOLS}
            if platform == "linux"
            else set()
        )
        with zipfile.ZipFile(archive_path) as bundle:
            invalid_modes = []
            for info in bundle.infolist():
                archived_mode = info.external_attr >> 16
                expected_mode = 0o755 if info.filename in native_members else 0o644
                if stat.S_IFMT(archived_mode) != stat.S_IFREG or stat.S_IMODE(archived_mode) != expected_mode:
                    invalid_modes.append((info.filename, f"{archived_mode:o}", f"{stat.S_IFREG | expected_mode:o}"))
        if invalid_modes:
            raise ReleaseValidationError(
                f"engine archive member modes do not match the exact release contract: {invalid_modes}"
            )
        _validate_spdx(
            extracted / SPDX_MEMBER,
            index_path=extracted / LICENSE_INDEX_MEMBER,
            platform=platform,
            accelerator=accelerator,
        )
        if run_smokes:
            if converter_python is None:
                raise ReleaseValidationError("packaged smoke validation requires an absolute converter interpreter")
            _run_packaged_smokes(
                extracted,
                platform=platform,
                converter_python=converter_python,
            )
        return inner


def _validate_rebuild_inputs(rebuild_inputs: object, label: str) -> dict[str, Any]:
    if not isinstance(rebuild_inputs, dict) or set(rebuild_inputs) != REBUILD_INPUT_FIELDS:
        raise ReleaseValidationError(f"rebuild inputs are invalid for {label}")
    digest_fields = REBUILD_INPUT_FIELDS - {
        "source_tree",
        "native_fixture_model_url",
        "native_fixture_license_url",
        "fim_fixture_model_url",
        "fim_fixture_model_revision",
        "fim_fixture_model_size_bytes",
        "fim_fixture_license_url",
    }
    if (
        not isinstance(rebuild_inputs["source_tree"], str)
        or COMMIT_PATTERN.fullmatch(rebuild_inputs["source_tree"]) is None
    ):
        raise ReleaseValidationError(f"source tree identity is invalid for {label}")
    if any(
        not isinstance(rebuild_inputs[field], str) or SHA256_PATTERN.fullmatch(rebuild_inputs[field]) is None
        for field in digest_fields
    ):
        raise ReleaseValidationError(f"measured material digest is invalid for {label}")
    expected_fim = {
        "fim_fixture_model_url": FIM_MODEL_URL,
        "fim_fixture_model_revision": FIM_MODEL_REVISION,
        "fim_fixture_model_sha256": FIM_MODEL_SHA256,
        "fim_fixture_model_size_bytes": FIM_MODEL_SIZE,
        "fim_fixture_license_url": FIM_LICENSE_URL,
        "fim_fixture_license_sha256": FIM_LICENSE_SHA256,
    }
    if any(rebuild_inputs[field] != value for field, value in expected_fim.items()):
        raise ReleaseValidationError(f"governed FIM fixture identity is invalid for {label}")
    return rebuild_inputs


def _validate_manifest_cuda_evidence(
    manifest: dict[str, Any],
    root: Path,
    *,
    identity: ReleaseIdentity | None = None,
) -> None:
    evidence = manifest.get("evidence")
    if not isinstance(evidence, list) or len(evidence) != 1 or not isinstance(evidence[0], dict):
        raise ReleaseValidationError("release manifest must bind exactly one CUDA certification asset")
    row = evidence[0]
    if set(row) != {"file", "sha256", "size_bytes"} or row.get("file") != CUDA_CERTIFICATION_ASSET:
        raise ReleaseValidationError("release manifest CUDA certification row is invalid")
    path = root / CUDA_CERTIFICATION_ASSET
    if (
        not path.is_file()
        or not isinstance(row.get("size_bytes"), int)
        or isinstance(row.get("size_bytes"), bool)
        or row["size_bytes"] <= 0
        or not isinstance(row.get("sha256"), str)
        or SHA256_PATTERN.fullmatch(row["sha256"]) is None
        or path.stat().st_size != row["size_bytes"]
        or sha256_file(path) != row["sha256"]
    ):
        raise ReleaseValidationError("release manifest does not match the CUDA certification bytes")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise ReleaseValidationError("release manifest provenance is invalid")
    repository = identity.repository if identity is not None else provenance.get("repository")
    source_commit = identity.source_commit if identity is not None else provenance.get("source_commit")
    run_id = identity.run_id if identity is not None else provenance.get("run_id")
    if (
        not isinstance(repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
        or not isinstance(source_commit, str)
        or COMMIT_PATTERN.fullmatch(source_commit) is None
        or not isinstance(run_id, str)
        or not run_id.isdigit()
    ):
        raise ReleaseValidationError("CUDA certification release identity is invalid")
    _validate_cuda_evidence(
        path,
        expected_commit=source_commit,
        expected_workflow_run_url=f"https://github.com/{repository}/actions/runs/{run_id}",
        require_certified=True,
    )
    _validate_cuda_certification_subject(
        _read_json_object(path, "CUDA certification evidence")["subject"],
        subject_directory=root,
    )


def merge_release_fragments(root: Path, output: Path, identity: ReleaseIdentity) -> dict[str, Any]:
    """Validate and merge the exact four per-leg release fragments.

    Args:
        root: Directory containing four archives and four fragments.
        output: Destination for the merged manifest.
        identity: Expected immutable workflow identity.

    Returns:
        Merged manifest object.

    Raises:
        ReleaseValidationError: If a fragment, archive, or cross-leg identity is invalid.
    """
    fragments = sorted(root.glob("manifest-*.json"))
    if len(fragments) != len(EXPECTED_LEGS):
        raise ReleaseValidationError(f"expected four manifest fragments, found {len(fragments)}")
    artifacts: list[dict[str, Any]] = []
    common: tuple[str, str, str] | None = None
    flags: dict[str, list[str]] = {}
    inputs: dict[str, dict[str, Any]] = {}
    for path in fragments:
        fragment = _read_json_object(path, "release manifest fragment")
        if set(fragment) != {"engine_version", "libllama_rev", "min_pkg_version", "artifacts", "provenance"}:
            raise ReleaseValidationError(f"manifest fragment fields are invalid: {path.name}")
        rows = fragment["artifacts"]
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ReleaseValidationError(f"manifest fragment must contain one artifact: {path.name}")
        artifact = rows[0]
        if set(artifact) != {"platform", "accel", "file", "sha256", "size_bytes"}:
            raise ReleaseValidationError(f"manifest fragment artifact fields are invalid: {path.name}")
        filename = artifact["file"]
        expected_selector = EXPECTED_LEGS.get(filename)
        if expected_selector is None or (artifact["platform"], artifact["accel"]) != expected_selector:
            raise ReleaseValidationError(f"unexpected or mismatched release artifact: {filename!r}")
        archive = root / filename
        if not archive.is_file() or archive.stat().st_size != artifact["size_bytes"]:
            raise ReleaseValidationError(f"outer size mismatch for {filename}")
        if sha256_file(archive) != artifact["sha256"]:
            raise ReleaseValidationError(f"outer digest mismatch for {filename}")
        validate_archive(archive, platform=expected_selector[0], accelerator=expected_selector[1])
        provenance = fragment["provenance"]
        if not isinstance(provenance, dict) or any(
            provenance.get(key) != value for key, value in identity.as_dict().items()
        ):
            raise ReleaseValidationError(f"workflow identity mismatch for {filename}")
        deterministic_flags = provenance.get("deterministic_flags")
        rebuild_inputs = provenance.get("rebuild_inputs")
        if not isinstance(deterministic_flags, list) or len(deterministic_flags) != len(set(deterministic_flags)):
            raise ReleaseValidationError(f"deterministic flags are invalid for {filename}")
        rebuild_inputs = _validate_rebuild_inputs(rebuild_inputs, filename)
        leg_common = (fragment["engine_version"], fragment["libllama_rev"], fragment["min_pkg_version"])
        if common is None:
            common = leg_common
        elif common != leg_common:
            raise ReleaseValidationError("release fragments disagree on version identity")
        artifacts.append(artifact)
        flags[filename] = deterministic_flags
        inputs[filename] = rebuild_inputs
    if {row["file"] for row in artifacts} != set(EXPECTED_LEGS):
        raise ReleaseValidationError("release fragments do not contain each expected artifact exactly once")
    if len({json.dumps(value, sort_keys=True) for value in inputs.values()}) != 1:
        raise ReleaseValidationError("release legs disagree on measured source and material identity")
    if common is None:
        raise ReleaseValidationError("release fragments are absent")
    version, llama_revision, minimum_package = common
    if identity.source_ref != f"refs/tags/v{version}":
        raise ReleaseValidationError("engine version does not match the immutable release tag")
    cuda_path = root / CUDA_CERTIFICATION_ASSET
    _validate_cuda_evidence(
        cuda_path,
        expected_commit=identity.source_commit,
        expected_workflow_run_url=(f"https://github.com/{identity.repository}/actions/runs/{identity.run_id}"),
        require_certified=True,
    )
    _validate_cuda_certification_subject(
        _read_json_object(cuda_path, "CUDA certification evidence")["subject"],
        subject_directory=root,
        require_manifest=True,
    )
    manifest = {
        "engine_version": version,
        "libllama_rev": llama_revision,
        "min_pkg_version": minimum_package,
        "artifacts": sorted(artifacts, key=lambda row: row["file"]),
        "evidence": [
            {
                "file": CUDA_CERTIFICATION_ASSET,
                "sha256": sha256_file(cuda_path),
                "size_bytes": cuda_path.stat().st_size,
            }
        ],
        "provenance": {
            **identity.as_dict(),
            "toolchain": {"rust": "1.88.0", "cuda": "12.4.1"},
            "deterministic_flags": flags,
            "rebuild_inputs": inputs,
        },
    }
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    inner_manifest_digests: dict[str, str] = {}
    for filename, (platform, accelerator) in EXPECTED_LEGS.items():
        with zipfile.ZipFile(root / filename) as archive:
            try:
                inner_bytes = archive.read("manifest.json")
            except KeyError as exc:
                raise ReleaseValidationError(f"release archive has no inner manifest: {filename}") from exc
        inner_manifest_digests[f"{platform}-{accelerator}"] = hashlib.sha256(inner_bytes).hexdigest()
    authority = {
        "schema_version": 1,
        "release": f"v{version}",
        "source_commit": identity.source_commit,
        "release_manifest_sha256": sha256_file(output),
        "inner_manifest_sha256_by_bundle": dict(sorted(inner_manifest_digests.items())),
    }
    (root / CONSUMER_AUTHORITY_ASSET).write_text(
        json.dumps(authority, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _validate_consumer_release_authority(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    authority = _read_json_object(root / CONSUMER_AUTHORITY_ASSET, "consumer release authority")
    if (
        set(authority)
        != {
            "schema_version",
            "release",
            "source_commit",
            "release_manifest_sha256",
            "inner_manifest_sha256_by_bundle",
        }
        or authority["schema_version"] != 1
    ):
        raise ReleaseValidationError("consumer release authority fields are invalid")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise ReleaseValidationError("consumer release authority has no manifest provenance")
    if (
        authority["release"] != f"v{manifest.get('engine_version')}"
        or authority["source_commit"] != provenance.get("source_commit")
        or authority["release_manifest_sha256"] != sha256_file(root / "manifest.json")
    ):
        raise ReleaseValidationError("consumer release authority disagrees with the peeled release identity")
    expected: dict[str, str] = {}
    for filename, (platform, accelerator) in EXPECTED_LEGS.items():
        with zipfile.ZipFile(root / filename) as archive:
            try:
                inner_bytes = archive.read("manifest.json")
            except KeyError as exc:
                raise ReleaseValidationError(f"release archive has no inner manifest: {filename}") from exc
        expected[f"{platform}-{accelerator}"] = hashlib.sha256(inner_bytes).hexdigest()
    if authority["inner_manifest_sha256_by_bundle"] != dict(sorted(expected.items())):
        raise ReleaseValidationError("consumer release authority does not bind the exact inner manifests")
    return authority


def _receipt_artifact_producer(name: str) -> tuple[str, str]:
    return {
        "tiny-model-f16": ("convert_hf_to_gguf", "f16"),
        "adapter": ("convert_lora_to_gguf", "adapter_gguf"),
        "tiny-model": ("imatrix", "imatrix_output"),
        "tiny-model-q4_0": ("quantize", "quantized"),
    }[name]


def _validate_bootstrap_evidence_archive(
    path: Path,
    *,
    bundle: str,
    tool_bytes: dict[str, bytes],
    protected_closure_bytes: bytes,
    protected_closure_objects_path: Path,
) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or any(
            PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name for name in names
        ):
            raise ReleaseValidationError(f"bootstrap evidence archive paths are unsafe: {bundle}")
        try:
            manifest = json.loads(archive.read("evidence-manifest.json"))
            execution = json.loads(archive.read("execution.json"))
        except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseValidationError(f"bootstrap evidence archive metadata is invalid: {bundle}") from exc
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "files"}
            or manifest["schema_version"] != 1
        ):
            raise ReleaseValidationError(f"bootstrap evidence archive manifest is invalid: {bundle}")
        rows = manifest["files"]
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or set(row) != {"path", "size_bytes", "sha256"} for row in rows
        ):
            raise ReleaseValidationError(f"bootstrap evidence archive manifest rows are invalid: {bundle}")
        expected_names = set(names) - {"evidence-manifest.json"}
        if {row["path"] for row in rows} != expected_names or len(rows) != len(expected_names):
            raise ReleaseValidationError(f"bootstrap evidence archive member set is invalid: {bundle}")
        object_prefix = "runtime/interpreter-closure-objects/"
        payloads: dict[str, bytes] = {}
        object_identities: dict[str, tuple[int, str]] = {}
        for row in rows:
            member = row["path"]
            if member.startswith(object_prefix):
                observed = hashlib.sha256()
                size = 0
                with archive.open(member) as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        observed.update(chunk)
                        size += len(chunk)
                digest = observed.hexdigest()
                object_identities[member] = (size, digest)
                if row["size_bytes"] != size or row["sha256"] != digest:
                    raise ReleaseValidationError(
                        f"bootstrap evidence closure object digest is invalid: {bundle}/{member}"
                    )
                continue
            payload = archive.read(member)
            payloads[member] = payload
            if row["size_bytes"] != len(payload) or row["sha256"] != hashlib.sha256(payload).hexdigest():
                raise ReleaseValidationError(f"bootstrap evidence archive member digest is invalid: {bundle}/{member}")
    if not isinstance(execution, dict) or set(execution) != {
        "schema_version",
        "cwd",
        "interpreter_closure",
        "bundle_tools",
        "steps",
    }:
        raise ReleaseValidationError(f"bootstrap evidence execution fields are invalid: {bundle}")
    if execution["schema_version"] != 2 or execution["cwd"] != "workspace":
        raise ReleaseValidationError(f"bootstrap evidence execution cwd is invalid: {bundle}")
    closure_record = execution["interpreter_closure"]
    closure_member = "runtime/interpreter-closure.json"
    closure_bytes = payloads.get(closure_member)
    if not closure_bytes:
        raise ReleaseValidationError(f"bootstrap evidence interpreter closure is missing: {bundle}")
    if closure_bytes != protected_closure_bytes:
        raise ReleaseValidationError(
            f"bootstrap evidence interpreter closure disagrees with protected authority: {bundle}"
        )
    try:
        closure = json.loads(closure_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(f"bootstrap evidence interpreter closure is invalid: {bundle}") from exc
    closure_fields = {
        "schema_version",
        "implementation",
        "version",
        "file_count",
        "size_bytes",
        "closure_sha256",
        "files",
    }
    closure_rows = closure.get("files") if isinstance(closure, dict) else None
    if (
        not isinstance(closure, dict)
        or set(closure) != closure_fields
        or closure["schema_version"] != 1
        or not isinstance(closure["implementation"], str)
        or not closure["implementation"]
        or not isinstance(closure["version"], str)
        or not closure["version"]
        or not isinstance(closure_rows, list)
        or not closure_rows
        or any(
            not isinstance(row, dict)
            or set(row) != {"path", "size_bytes", "sha256"}
            or not isinstance(row["path"], str)
            or PurePosixPath(row["path"]).is_absolute()
            or ".." in PurePosixPath(row["path"]).parts
            or "\\" in row["path"]
            or not isinstance(row["size_bytes"], int)
            or row["size_bytes"] < 0
            or SHA256_PATTERN.fullmatch(str(row["sha256"])) is None
            for row in closure_rows
        )
    ):
        raise ReleaseValidationError(f"bootstrap evidence interpreter closure schema is invalid: {bundle}")
    closure_canonical = "".join(
        f"{row['path']}\0{row['size_bytes']}\0{row['sha256']}\n" for row in closure_rows
    ).encode("utf-8")
    closure_by_path = {row["path"]: row for row in closure_rows}
    unique_object_sizes: dict[str, int] = {}
    for row in closure_rows:
        digest = row["sha256"]
        size = row["size_bytes"]
        if digest in unique_object_sizes and unique_object_sizes[digest] != size:
            raise ReleaseValidationError(f"bootstrap evidence closure object alias is invalid: {bundle}/{digest}")
        unique_object_sizes[digest] = size
    expected_object_identities = {
        f"{object_prefix}{digest}": (size, digest) for digest, size in unique_object_sizes.items()
    }
    if object_identities != expected_object_identities:
        raise ReleaseValidationError(f"bootstrap evidence closure object mapping is invalid: {bundle}")
    try:
        with zipfile.ZipFile(protected_closure_objects_path) as protected_archive:
            protected_infos = [info for info in protected_archive.infolist() if not info.is_dir()]
            protected_names = [info.filename for info in protected_infos]
            if len(protected_names) != len(set(protected_names)) or set(protected_names) != {
                f"objects/{digest}" for digest in unique_object_sizes
            }:
                raise ReleaseValidationError(f"protected interpreter closure object set is invalid: {bundle}")
            for info in protected_infos:
                digest = info.filename.removeprefix("objects/")
                observed = hashlib.sha256()
                size = 0
                with protected_archive.open(info) as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        observed.update(chunk)
                        size += len(chunk)
                if (size, observed.hexdigest()) != (unique_object_sizes[digest], digest):
                    raise ReleaseValidationError(
                        f"protected interpreter closure object bytes are invalid: {bundle}/{digest}"
                    )
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseValidationError(f"protected interpreter closure object archive is unreadable: {bundle}") from exc
    if (
        closure_rows != sorted(closure_rows, key=lambda row: row["path"])
        or len({row["path"] for row in closure_rows}) != len(closure_rows)
        or closure["file_count"] != len(closure_rows)
        or closure["size_bytes"] != sum(int(row["size_bytes"]) for row in closure_rows)
        or closure["closure_sha256"] != hashlib.sha256(closure_canonical).hexdigest()
        or closure_record
        != {
            "kind": "file",
            "member": closure_member,
            "size_bytes": len(closure_bytes),
            "sha256": hashlib.sha256(closure_bytes).hexdigest(),
            "file_count": closure["file_count"],
            "closure_sha256": closure["closure_sha256"],
            "objects_prefix": object_prefix,
            "object_count": len(unique_object_sizes),
            "object_size_bytes": sum(unique_object_sizes.values()),
        }
    ):
        raise ReleaseValidationError(f"bootstrap evidence interpreter closure identity is invalid: {bundle}")
    expected_tools = {
        tool: {
            "member": export_tool_member(tool, platform="windows"),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for tool, payload in tool_bytes.items()
    }
    if execution["bundle_tools"] != expected_tools:
        raise ReleaseValidationError(f"bootstrap evidence converter script or tool identity is invalid: {bundle}")
    expected_steps = {
        "convert_hf_to_gguf": {
            "executable": "converter_python",
            "argv": [
                "bundle-tool:convert_hf_to_gguf",
                "object:merged_model",
                "--outfile",
                "object:f16",
                "--outtype",
                "f16",
            ],
            "inputs": ["converter_python", "convert_hf_to_gguf", "merged_model"],
            "outputs": ["f16"],
        },
        "convert_lora_to_gguf": {
            "executable": "converter_python",
            "argv": [
                "bundle-tool:convert_lora_to_gguf",
                "--base",
                "object:merged_model",
                "--outfile",
                "object:adapter_gguf",
                "object:lora_adapter",
            ],
            "inputs": ["converter_python", "convert_lora_to_gguf", "merged_model", "lora_adapter"],
            "outputs": ["adapter_gguf"],
        },
        "imatrix": {
            "executable": "bundle-tool:imatrix",
            "argv": [
                "-m",
                "object:f16",
                "-f",
                "object:corpus",
                "-o",
                "object:imatrix_output",
                "-ngl",
                "0",
            ],
            "inputs": ["imatrix", "f16", "corpus"],
            "outputs": ["imatrix_output"],
        },
        "quantize": {
            "executable": "bundle-tool:quantize",
            "argv": ["--imatrix", "object:imatrix_output", "object:f16", "object:quantized", "Q4_0"],
            "inputs": ["quantize", "imatrix_output", "f16"],
            "outputs": ["quantized"],
        },
    }
    steps = execution["steps"]
    if not isinstance(steps, dict) or set(steps) != set(expected_steps):
        raise ReleaseValidationError(f"bootstrap evidence step set is invalid: {bundle}")
    claimed_members = {"execution.json", closure_member, *object_identities}
    first_inputs: dict[str, tuple[Any, ...]] = {}
    producer_outputs: dict[str, tuple[Any, ...]] = {}

    def validate_snapshot(record: Any, *, prefix: str, description: str) -> tuple[Any, ...]:
        if not isinstance(record, dict):
            raise ReleaseValidationError(f"bootstrap evidence snapshot is invalid: {bundle}/{description}")
        if record.get("kind") == "file" and set(record) == {"kind", "member", "size_bytes", "sha256"}:
            member = record["member"]
            payload = payloads.get(member) if isinstance(member, str) and member.startswith(f"{prefix}/") else None
            if (
                not payload
                or record["size_bytes"] != len(payload)
                or record["sha256"] != hashlib.sha256(payload).hexdigest()
            ):
                raise ReleaseValidationError(f"bootstrap evidence file snapshot is invalid: {bundle}/{description}")
            claimed_members.add(member)
            return ("file", None, len(payload), record["sha256"])
        if record.get("kind") == "tree" and set(record) == {
            "kind",
            "prefix",
            "file_count",
            "size_bytes",
            "sha256",
        }:
            tree_prefix = record["prefix"]
            if (
                not isinstance(tree_prefix, str)
                or not tree_prefix.startswith(f"{prefix}/")
                or not tree_prefix.endswith("/")
            ):
                raise ReleaseValidationError(
                    f"bootstrap evidence tree snapshot prefix is invalid: {bundle}/{description}"
                )
            tree_rows = [
                (member.removeprefix(tree_prefix), len(payload), hashlib.sha256(payload).hexdigest())
                for member, payload in sorted(payloads.items())
                if member.startswith(tree_prefix)
            ]
            canonical = "".join(f"{relative}\0{size}\0{digest}\n" for relative, size, digest in tree_rows).encode(
                "utf-8"
            )
            if (
                not tree_rows
                or record["file_count"] != len(tree_rows)
                or record["size_bytes"] != sum(row[1] for row in tree_rows)
                or record["sha256"] != hashlib.sha256(canonical).hexdigest()
            ):
                raise ReleaseValidationError(f"bootstrap evidence tree snapshot is invalid: {bundle}/{description}")
            claimed_members.update(member for member in payloads if member.startswith(tree_prefix))
            return ("tree", len(tree_rows), record["size_bytes"], record["sha256"])
        raise ReleaseValidationError(f"bootstrap evidence snapshot fields are invalid: {bundle}/{description}")

    for ordinal, (name, expected) in enumerate(expected_steps.items(), start=1):
        step = steps[name]
        result_fields = {"returncode", "stdout_sha256", "stderr_sha256"}
        if (
            not isinstance(step, dict)
            or set(step)
            != {*expected, "input_snapshots", "output_snapshots", "interpreter_closure_sha256", "cwd", *result_fields}
            or {key: step[key] for key in expected} != expected
            or step["cwd"] != "workspace"
            or step["interpreter_closure_sha256"] != closure["closure_sha256"]
            or step["returncode"] != 0
            or any(SHA256_PATTERN.fullmatch(str(step[key])) is None for key in result_fields - {"returncode"})
            or not isinstance(step["input_snapshots"], dict)
            or set(step["input_snapshots"]) != set(expected["inputs"])
            or not isinstance(step["output_snapshots"], dict)
            or set(step["output_snapshots"]) != set(expected["outputs"])
        ):
            raise ReleaseValidationError(f"bootstrap evidence argv, cwd, or associations are invalid: {bundle}/{name}")
        step_prefix = f"steps/{ordinal:02d}-{name}"
        for input_name, record in step["input_snapshots"].items():
            identity = validate_snapshot(
                record, prefix=f"{step_prefix}/inputs/{input_name}", description=f"{name}/{input_name}"
            )
            if input_name in first_inputs and first_inputs[input_name] != identity:
                raise ReleaseValidationError(f"bootstrap evidence reused input identity changed: {bundle}/{input_name}")
            first_inputs.setdefault(input_name, identity)
            if input_name in producer_outputs and producer_outputs[input_name] != identity:
                raise ReleaseValidationError(
                    f"bootstrap evidence producer/consumer bytes disagree: {bundle}/{input_name}"
                )
            if input_name in tool_bytes:
                member = record.get("member")
                if not isinstance(member, str) or payloads.get(member) != tool_bytes[input_name]:
                    raise ReleaseValidationError(
                        f"bootstrap evidence signed tool input bytes disagree: {bundle}/{input_name}"
                    )
            if input_name == "converter_python":
                member = record.get("member")
                runtime_row = closure_by_path.get(f"runtime/{PurePosixPath(str(member)).name}")
                if (
                    record.get("kind") != "file"
                    or runtime_row is None
                    or record["size_bytes"] != runtime_row["size_bytes"]
                    or record["sha256"] != runtime_row["sha256"]
                ):
                    raise ReleaseValidationError(
                        f"bootstrap evidence converter interpreter disagrees with its closure: {bundle}"
                    )
        for output_name, record in step["output_snapshots"].items():
            identity = validate_snapshot(
                record, prefix=f"{step_prefix}/outputs/{output_name}", description=f"{name}/{output_name}"
            )
            if output_name in producer_outputs:
                raise ReleaseValidationError(f"bootstrap evidence output was produced twice: {bundle}/{output_name}")
            producer_outputs[output_name] = identity
    if expected_names != claimed_members:
        raise ReleaseValidationError(f"bootstrap evidence archive contains unclaimed snapshot bytes: {bundle}")
    return execution


def validate_bootstrap_receipts(
    *,
    release_root: Path,
    receipts_root: Path,
    closures_root: Path,
    expected_commit: str,
) -> list[dict[str, Any]]:
    """Bind both Windows bootstrap receipts to signed archive bytes and identity."""
    if COMMIT_PATTERN.fullmatch(expected_commit) is None:
        raise ReleaseValidationError("bootstrap receipt expected commit is invalid")
    expected_receipts = {
        f"{bundle}/release-bootstrap-export-toolchain-{bundle}.json": (bundle, accelerator)
        for bundle, accelerator in (("windows-cpu", "cpu"), ("windows-cuda", "cuda"))
    }
    expected_evidence = {
        f"{bundle}/release-bootstrap-export-toolchain-{bundle}.evidence.zip"
        for bundle in ("windows-cpu", "windows-cuda")
    }
    actual_paths = (
        {path.relative_to(receipts_root).as_posix() for path in receipts_root.rglob("*") if path.is_file()}
        if receipts_root.is_dir()
        else set()
    )
    if actual_paths != set(expected_receipts) | expected_evidence:
        raise ReleaseValidationError(f"bootstrap receipt set is not exact: {sorted(actual_paths)}")
    expected_closures = {
        f"{bundle}/signed-validator-interpreter-closure-{bundle}{suffix}"
        for bundle in ("windows-cpu", "windows-cuda")
        for suffix in (".json", ".objects.zip")
    }
    actual_closures = (
        {path.relative_to(closures_root).as_posix() for path in closures_root.rglob("*") if path.is_file()}
        if closures_root.is_dir()
        else set()
    )
    if actual_closures != expected_closures:
        raise ReleaseValidationError(f"protected interpreter closure set is not exact: {sorted(actual_closures)}")
    validated: list[dict[str, Any]] = []
    artifact_files = {
        "tiny-model-f16": "tiny-model-f16.gguf",
        "adapter": "adapter.gguf",
        "tiny-model": "tiny-model.imatrix",
        "tiny-model-q4_0": "tiny-model-q4_0.gguf",
    }
    for relative, (bundle, accelerator) in expected_receipts.items():
        archive_path = release_root / f"amw-engine-{bundle}.zip"
        if not archive_path.is_file():
            raise ReleaseValidationError(f"bootstrap receipt archive is missing: {archive_path.name}")
        with zipfile.ZipFile(archive_path) as archive:
            try:
                inner_bytes = archive.read("manifest.json")
                tool_bytes = {
                    tool: archive.read(export_tool_member(tool, platform="windows")) for tool in EXPORT_TOOL_MEMBERS
                }
            except KeyError as exc:
                raise ReleaseValidationError(f"bootstrap receipt archive is incomplete: {archive_path.name}") from exc
        receipt = _read_json_object(receipts_root / relative, f"bootstrap receipt {bundle}")
        if set(receipt) != {
            "schema_version",
            "status",
            "trust",
            "tools",
            "interpreter_closure",
            "steps",
            "artifacts",
            "evidence_archive",
        }:
            raise ReleaseValidationError(f"bootstrap receipt fields are invalid: {bundle}")
        if receipt["schema_version"] != 1 or receipt["status"] != "passed":
            raise ReleaseValidationError(f"bootstrap receipt did not pass: {bundle}")
        expected_trust = {
            "mode": "release-bootstrap",
            "source_commit": expected_commit,
            "platform": "windows",
            "accelerator": accelerator,
            "inner_manifest_sha256": hashlib.sha256(inner_bytes).hexdigest(),
        }
        if receipt["trust"] != expected_trust:
            raise ReleaseValidationError(f"bootstrap receipt trust identity is invalid: {bundle}")
        tools = receipt["tools"]
        if not isinstance(tools, dict) or set(tools) != set(EXPORT_TOOL_MEMBERS):
            raise ReleaseValidationError(f"bootstrap receipt tool set is invalid: {bundle}")
        for tool, payload in tools.items():
            expected_member = export_tool_member(tool, platform="windows")
            if (
                not isinstance(payload, dict)
                or set(payload) != {"path", "sha256"}
                or PureWindowsPath(str(payload["path"])).name != expected_member
                or payload["sha256"] != hashlib.sha256(tool_bytes[tool]).hexdigest()
            ):
                raise ReleaseValidationError(f"bootstrap receipt tool binding is invalid: {bundle}/{tool}")
        evidence_path = receipts_root / bundle / f"release-bootstrap-export-toolchain-{bundle}.evidence.zip"
        evidence_row = receipt["evidence_archive"]
        if (
            not isinstance(evidence_row, dict)
            or set(evidence_row) != {"file", "size_bytes", "sha256"}
            or evidence_row["file"] != evidence_path.name
            or evidence_row["size_bytes"] != evidence_path.stat().st_size
            or evidence_row["sha256"] != sha256_file(evidence_path)
        ):
            raise ReleaseValidationError(f"bootstrap receipt evidence archive binding is invalid: {bundle}")
        execution = _validate_bootstrap_evidence_archive(
            evidence_path,
            bundle=bundle,
            tool_bytes=tool_bytes,
            protected_closure_bytes=(
                closures_root / bundle / f"signed-validator-interpreter-closure-{bundle}.json"
            ).read_bytes(),
            protected_closure_objects_path=(
                closures_root / bundle / f"signed-validator-interpreter-closure-{bundle}.objects.zip"
            ),
        )
        if receipt["steps"] != execution["steps"]:
            raise ReleaseValidationError(f"bootstrap receipt execution contract is substituted: {bundle}")
        closure = execution["interpreter_closure"]
        if receipt["interpreter_closure"] != {
            "file_count": closure["file_count"],
            "closure_sha256": closure["closure_sha256"],
            "object_count": closure["object_count"],
        }:
            raise ReleaseValidationError(f"bootstrap receipt interpreter closure is substituted: {bundle}")
        artifacts = receipt["artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != set(artifact_files):
            raise ReleaseValidationError(f"bootstrap receipt artifact set is invalid: {bundle}")
        artifact_hashes: set[str] = set()
        for name, expected_file in artifact_files.items():
            payload = artifacts[name]
            producer_step, producer_output = _receipt_artifact_producer(name)
            snapshot = execution["steps"][producer_step]["output_snapshots"][producer_output]
            if (
                not isinstance(payload, dict)
                or set(payload) != {"file", "size_bytes", "sha256"}
                or payload["file"] != expected_file
                or not isinstance(payload["size_bytes"], int)
                or payload["size_bytes"] <= 0
                or SHA256_PATTERN.fullmatch(str(payload["sha256"])) is None
                or snapshot.get("kind") != "file"
                or PurePosixPath(str(snapshot.get("member"))).name != expected_file
                or payload["size_bytes"] != snapshot["size_bytes"]
                or payload["sha256"] != snapshot["sha256"]
            ):
                raise ReleaseValidationError(f"bootstrap receipt artifact result is invalid: {bundle}/{name}")
            artifact_hashes.add(payload["sha256"])
        if len(artifact_hashes) != len(artifact_files):
            raise ReleaseValidationError(f"bootstrap receipt artifact hashes are not distinct: {bundle}")
        validated.append(receipt)
    return validated


def validate_release_directory(root: Path, identity: ReleaseIdentity) -> dict[str, Any]:
    """Validate a re-downloaded release directory against its merged manifest.

    Args:
        root: Directory containing the published manifest and archives.
        identity: Expected immutable workflow identity.

    Returns:
        Parsed and validated merged manifest.

    Raises:
        ReleaseValidationError: If the asset set, identity, or archive binding is invalid.
    """
    manifest = _read_json_object(root / "manifest.json", "published release manifest")
    if set(manifest) != {
        "engine_version",
        "libllama_rev",
        "min_pkg_version",
        "artifacts",
        "evidence",
        "provenance",
    }:
        raise ReleaseValidationError("published release manifest fields are invalid")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != RELEASE_ASSET_NAMES or any(path.is_dir() for path in root.iterdir()):
        raise ReleaseValidationError(f"published release asset set is not flat and exact: {sorted(actual)}")
    provenance = manifest["provenance"]
    if not isinstance(provenance, dict) or any(
        provenance.get(key) != value for key, value in identity.as_dict().items()
    ):
        raise ReleaseValidationError("published release workflow identity is invalid")
    rows = manifest["artifacts"]
    if not isinstance(rows, list) or len(rows) != len(EXPECTED_LEGS):
        raise ReleaseValidationError("published release does not contain four artifact rows")
    observed: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"platform", "accel", "file", "sha256", "size_bytes"}:
            raise ReleaseValidationError("published artifact row fields are invalid")
        name = row["file"]
        selector = EXPECTED_LEGS.get(name)
        if selector is None or name in observed or (row["platform"], row["accel"]) != selector:
            raise ReleaseValidationError(f"published artifact selector is invalid: {name!r}")
        archive = root / name
        if archive.stat().st_size != row["size_bytes"] or sha256_file(archive) != row["sha256"]:
            raise ReleaseValidationError(f"published digest or size mismatch for {name}")
        validate_archive(archive, platform=selector[0], accelerator=selector[1])
        observed.add(name)
    if observed != set(EXPECTED_LEGS):
        raise ReleaseValidationError("published release artifact set is incomplete")
    rebuild_inputs = provenance.get("rebuild_inputs")
    if not isinstance(rebuild_inputs, dict) or set(rebuild_inputs) != set(EXPECTED_LEGS):
        raise ReleaseValidationError("published release rebuild-input map is incomplete")
    validated_inputs = {name: _validate_rebuild_inputs(value, name) for name, value in rebuild_inputs.items()}
    if len({json.dumps(value, sort_keys=True) for value in validated_inputs.values()}) != 1:
        raise ReleaseValidationError("published release legs disagree on rebuild inputs")
    _validate_manifest_cuda_evidence(manifest, root, identity=identity)
    _validate_consumer_release_authority(root, manifest)
    return manifest


def validate_publication_prerequisites(
    *,
    workflow_path: Path,
    ledger_path: Path,
    cuda_evidence_path: Path,
    expected_commit: str,
    expected_workflow_run_url: str,
    expected_workflow_run_attempt: int,
    expected_runner_id: int,
    expected_runner_name: str,
    expected_machine_id_sha256: str,
    expected_service_account: str,
    expected_work_directory: str,
    certifier_public_key_der_base64: str,
) -> None:
    """Require measured size budgets and same-commit GPU/offload certification.

    Args:
        workflow_path: Engine workflow YAML path.
        ledger_path: Committed size baseline ledger.
        cuda_evidence_path: CUDA certification result downloaded from the governed job.
        expected_commit: Source commit being considered for publication.
        expected_workflow_run_url: Immutable URL for the current publication workflow run.

    Raises:
        ReleaseValidationError: If any hosted publication prerequisite is absent.
    """
    validate_workflow_size_bindings(workflow_path)
    validate_workflow_export_toolchain_bindings(workflow_path)
    validate_workflow_publication_bindings(workflow_path)
    load_size_ledger(ledger_path, require_measured=True)
    validate_cuda_certification_result(
        cuda_evidence_path,
        expected_commit=expected_commit,
        expected_workflow_run_url=expected_workflow_run_url,
        expected_workflow_run_attempt=expected_workflow_run_attempt,
        expected_runner_id=expected_runner_id,
        expected_runner_name=expected_runner_name,
        expected_machine_id_sha256=expected_machine_id_sha256,
        expected_service_account=expected_service_account,
        expected_work_directory=expected_work_directory,
        certifier_public_key_der_base64=certifier_public_key_der_base64,
    )


def validate_workflow_publication_bindings(workflow_path: Path) -> None:
    """Verify that publication consumes only the protected same-run CUDA result.

    Args:
        workflow_path: Engine workflow YAML path.

    Raises:
        ReleaseValidationError: If the governed CUDA-result or draft-asset channel is weakened.
    """
    text = workflow_path.read_text(encoding="utf-8")
    cuda_job = re.search(
        r"  cuda-certification:\n(?P<body>.*?)(?=\n  [a-zA-Z0-9_-]+:)",
        text,
        re.DOTALL,
    )
    required_cuda_job_markers = (
        "    runs-on: [self-hosted, vetinari-engine-gpu, linux, x64, cuda-12-4]\n",
        "    environment: engine-release-cuda-certification\n",
        "          name: engine-linux-cuda\n",
        "          CERTIFIER_SHA256: ${{ vars.ENGINE_CUDA_CERTIFIER_SHA256 }}\n",
        '          certifier="/opt/vetinari/bin/amw-engine-cuda-certifier"\n',
        "          /opt/vetinari/bin/amw-engine-cuda-certifier \\\n",
        "            --archive cuda-certification/input/amw-engine-linux-cuda.zip \\\n",
        "            --manifest cuda-certification/input/manifest-linux-cuda.json \\\n",
        "            --output cuda-certification/result/cuda-device-certification.json \\\n",
        '            --workflow-run-attempt "${{ github.run_attempt }}" \\\n',
        "            --runner-label self-hosted \\\n",
        "            --runner-label vetinari-engine-gpu \\\n",
        "          name: engine-cuda-device-certification\n",
        "          path: cuda-certification/result/cuda-device-certification.json\n",
    )
    binding_job = re.search(
        r"  cuda-certification-binding:\n(?P<body>.*?)(?=\n  [a-zA-Z0-9_-]+:)",
        text,
        re.DOTALL,
    )
    required_binding_markers = (
        "    needs: cuda-certification\n",
        "    runs-on: ubuntu-latest\n",
        "    environment: engine-release-cuda-certification\n",
        "      actions: read\n",
        "          GH_TOKEN: ${{ github.token }}\n",
        "          RUNNER_AUDIT_TOKEN: ${{ secrets.ENGINE_RUNNER_AUDIT_TOKEN }}\n",
        "          name: engine-cuda-device-certification\n",
        "          name: engine-linux-cuda\n",
        "          path: cuda-certification/subject\n",
        '          expected = {"amw-engine-linux-cuda.zip", "manifest-linux-cuda.json"}\n',
        '            "repos/${{ github.repository }}/actions/runs/${{ github.run_id }}/attempts/${{ github.run_attempt }}/jobs?per_page=100" \\\n',
        '            "repos/${{ github.repository }}/actions/runners?per_page=100" \\\n',
        "          EXPECTED_RUNNER_ID: ${{ vars.ENGINE_CUDA_CERTIFICATION_RUNNER_ID }}\n",
        "          EXPECTED_RUNNER_NAME: ${{ vars.ENGINE_CUDA_CERTIFICATION_RUNNER_NAME }}\n",
        "          EXPECTED_MACHINE_ID_SHA256: ${{ vars.ENGINE_CUDA_HOST_ID_SHA256 }}\n",
        "          EXPECTED_SERVICE_ACCOUNT: ${{ vars.ENGINE_CUDA_SERVICE_ACCOUNT }}\n",
        "          EXPECTED_WORK_DIRECTORY: ${{ vars.ENGINE_CUDA_WORK_DIRECTORY }}\n",
        "          CERTIFIER_PUBLIC_KEY_DER_BASE64: ${{ vars.ENGINE_CUDA_CERTIFIER_PUBLIC_KEY_DER_BASE64 }}\n",
        "          python scripts/validate_engine_release.py bind-cuda-runner \\\n",
        "            --runner-metadata cuda-certification/metadata/runner-inventory.json \\\n",
        "            --workflow-jobs cuda-certification/metadata/workflow-jobs.json \\\n",
        "            --subject-directory cuda-certification/subject \\\n",
        '            --expected-runner-id "$EXPECTED_RUNNER_ID" \\\n',
        '            --expected-workflow-run-attempt "${{ github.run_attempt }}"\n',
        '            --certifier-public-key-der-base64 "$CERTIFIER_PUBLIC_KEY_DER_BASE64" \\\n',
        "            --output cuda-certification/result/cuda-certification.json \\\n",
        "          python scripts/validate_engine_release.py cuda-result \\\n",
        "          name: engine-cuda-certification\n",
        "          path: cuda-certification/result/cuda-certification.json\n",
    )
    required_cuda_consumer_markers = (
        "    needs: [supply-chain, build, cuda-certification-binding]\n",
        "    environment: engine-release-cuda-certification\n",
        "            --cuda-evidence cuda-certification/cuda-certification.json \\\n",
        '            --expected-runner-id "$EXPECTED_RUNNER_ID" \\\n',
        '            --expected-workflow-run-attempt "${{ github.run_attempt }}" \\\n',
        '            --certifier-public-key-der-base64 "$CERTIFIER_PUBLIC_KEY_DER_BASE64"\n',
        '            --expected-work-directory "$EXPECTED_WORK_DIRECTORY" \\\n',
    )
    if (
        cuda_job is None
        or any(marker not in cuda_job.group("body") for marker in required_cuda_job_markers)
        or binding_job is None
        or any(marker not in binding_job.group("body") for marker in required_binding_markers)
        or any(marker not in text for marker in required_cuda_consumer_markers)
    ):
        raise ReleaseValidationError("engine workflow does not preserve the governed same-run CUDA evidence channel")
    prerequisite_job = re.search(
        r"  publication-prerequisites:\n(?P<body>.*?)(?=\n  [a-zA-Z0-9_-]+:)",
        text,
        re.DOTALL,
    )
    if prerequisite_job is None:
        raise ReleaseValidationError("engine workflow has no publication prerequisite job")
    prerequisite_body = prerequisite_job.group("body")
    if (
        "docs/reference/engine-cuda-certification.json" in prerequisite_body
        or "name: engine-cuda-certification" not in prerequisite_body
        or "path: cuda-certification" not in prerequisite_body
    ):
        raise ReleaseValidationError("publication does not consume only the governed same-run CUDA result")
    publish_job = re.search(
        r"  publish:\n(?P<body>.*?)(?=\n  [a-zA-Z0-9_-]+:)",
        text,
        re.DOTALL,
    )
    publisher_markers = (
        "    environment: engine-release-publish\n",
        "    concurrency:\n",
        "      group: engine-release-publish-${{ github.repository_id }}-${{ github.ref_name }}\n",
        "      cancel-in-progress: false\n",
        "          EXCLUSIVE_WRITER_ENABLED: ${{ vars.ENGINE_RELEASE_EXCLUSIVE_WRITER_ENABLED }}\n",
        "          EXPECTED_ACTOR: ${{ vars.ENGINE_RELEASE_EXPECTED_ACTOR }}\n",
        "          EXPECTED_UPLOADER: ${{ vars.ENGINE_RELEASE_EXPECTED_UPLOADER }}\n",
        "          python release-validator/validate_engine_release.py bind-draft \\\n",
        '            --github-env "$GITHUB_ENV"\n',
        '              "https://uploads.github.com/repos/$REPOSITORY/releases/$RELEASE_ID/assets?name=$name" >/dev/null\n',
        "          python release-validator/validate_engine_release.py capture-draft-assets \\\n",
        "      - name: Publish the bound release ID without tag re-resolution\n",
        "          gh api --method PATCH -H 'X-GitHub-Api-Version: 2026-03-10' \\\n",
        '            "repos/$REPOSITORY/releases/$RELEASE_ID" > publish-response-by-id.json\n',
        "      - name: Prove same-ID immutable publication and tag mapping\n",
        "          python release-validator/validate_engine_release.py verify-published \\\n",
        "          name: engine-release-validator\n",
        "          path: release-validator\n",
    )
    if publish_job is None or any(marker not in publish_job.group("body") for marker in publisher_markers):
        raise ReleaseValidationError("engine publisher does not preserve the ID-bound exclusive critical window")
    publish_body = publish_job.group("body")
    banned_tag_mutations = ("gh release upload", "gh release edit", "/releases/tags/$RELEASE_TAG/assets")
    if any(marker in publish_body for marker in banned_tag_mutations):
        raise ReleaseValidationError("engine publisher mutates a tag-resolved release instead of the bound release ID")
    bind_index = publish_body.index("      - name: Bind one exact empty draft release ID\n")
    capture_index = publish_body.index("      - name: Capture exact uploaded asset IDs and identities\n")
    verify_index = publish_body.index("      - name: Revalidate the bound release ID immediately before publish\n")
    patch_index = publish_body.index("      - name: Publish the bound release ID without tag re-resolution\n")
    proof_index = publish_body.index("      - name: Prove same-ID immutable publication and tag mapping\n")
    if not bind_index < capture_index < verify_index < patch_index < proof_index:
        raise ReleaseValidationError("engine publisher release-ID critical window ordering is invalid")
    verify_header = "      - name: Revalidate the bound release ID immediately before publish\n"
    patch_header = "      - name: Publish the bound release ID without tag re-resolution\n"
    if (
        "      - name:" in publish_body[verify_index + len(verify_header) : patch_index]
        or "      - name:" in publish_body[patch_index + len(patch_header) : proof_index]
    ):
        raise ReleaseValidationError("engine publisher ID revalidation and immutable proof are not immediate")
    cuda_asset_markers = (
        "          name: engine-cuda-certification\n          path: release-assets\n",
        "            release-assets/consumer-release-authority.json\n",
        "            release-assets/cuda-certification.json\n",
        '[source / "consumer-release-authority.json", source / "cuda-certification.json", source / "manifest.json"]',
        '"consumer-release-authority.json",\n',
        '"cuda-certification.json",\n',
        "release-assets/consumer-release-authority.json release-assets/cuda-certification.json release-assets/manifest.json; do\n",
        "--pattern consumer-release-authority.json --pattern cuda-certification.json --pattern manifest.json --dir verified-release\n",
        "for asset in verified-release/consumer-release-authority.json verified-release/cuda-certification.json verified-release/manifest.json; do\n",
    )
    if any(marker not in text for marker in cuda_asset_markers):
        raise ReleaseValidationError("CUDA certification is not bound, attested, published, and re-downloaded")
    workflow = _load_workflow(workflow_path)
    jobs = workflow.get("jobs")
    parsed_publish = jobs.get("publish") if isinstance(jobs, dict) else None
    parsed_steps = parsed_publish.get("steps") if isinstance(parsed_publish, dict) else None
    if not isinstance(parsed_steps, list):
        raise ReleaseValidationError("engine publisher steps are not parseable")
    revalidate_step = next(
        (
            step
            for step in parsed_steps
            if isinstance(step, dict)
            and step.get("name") == "Revalidate the bound release ID immediately before publish"
        ),
        None,
    )
    patch_step = next(
        (
            step
            for step in parsed_steps
            if isinstance(step, dict) and step.get("name") == "Publish the bound release ID without tag re-resolution"
        ),
        None,
    )
    if not isinstance(revalidate_step, dict) or not isinstance(patch_step, dict):
        raise ReleaseValidationError("publisher final revalidation or PATCH step is missing")
    admin_token_env = "ADMIN_GH_" + "TOKEN"
    github_token_env = "GH_" + "TOKEN"
    _validate_mandatory_workflow_step(
        revalidate_step,
        label="final bound-draft revalidation",
        expected_run=(
            f'test -n "${admin_token_env}" || {{ echo "ENGINE_RELEASE_ADMIN_TOKEN is required for the immutable-release preflight" >&2; exit 1; }}\n'
            f'test "$({github_token_env}="${admin_token_env}" gh api -H \'X-GitHub-Api-Version: 2026-03-10\' '
            '"repos/$REPOSITORY/immutable-releases" --jq .enabled)" = "true"\n'
            'rows="$(git ls-remote --exit-code "https://github.com/$REPOSITORY.git" '
            '"refs/tags/$RELEASE_TAG" "refs/tags/$RELEASE_TAG^{}")"\n'
            'peeled="$(printf \'%s\\n\' "$rows" | awk -v ref="refs/tags/$RELEASE_TAG^{}" '
            "'$2 == ref {print $1}')\"\n"
            'if [[ -z "$peeled" ]]; then\n'
            '  peeled="$(printf \'%s\\n\' "$rows" | awk -v ref="refs/tags/$RELEASE_TAG" '
            "'$2 == ref {print $1}')\"\n"
            "fi\n"
            '[[ "$peeled" =~ ^[0-9a-f]{40}$ && "$peeled" == "$EXPECTED_COMMIT" ]] || {\n'
            '  echo "peeled remote tag $RELEASE_TAG changed after asset upload" >&2\n'
            "  exit 1\n"
            "}\n"
            "gh api -H 'X-GitHub-Api-Version: 2026-03-10' \\\n"
            '  "repos/$REPOSITORY/releases/$RELEASE_ID" > prepublish-release-by-id.json\n'
            "python release-validator/validate_engine_release.py verify-bound-draft \\\n"
            "  --root release-assets \\\n"
            "  --metadata prepublish-release-by-id.json \\\n"
            "  --binding release-binding-assets.json"
        ),
        expected_env={
            "ADMIN_GH_TOKEN": "${{ secrets.ENGINE_RELEASE_ADMIN_TOKEN }}",
            "GH_TOKEN": "${{ github.token }}",
            "EXPECTED_COMMIT": "${{ github.sha }}",
            "RELEASE_TAG": "${{ github.ref_name }}",
            "REPOSITORY": "${{ github.repository }}",
        },
        expected_shell="bash",
    )
    if parsed_steps.index(patch_step) != parsed_steps.index(revalidate_step) + 1:
        raise ReleaseValidationError("final bound-draft revalidation must run immediately before PATCH")


def _load_workflow(workflow_path: Path) -> dict[str, Any]:
    """Parse a workflow without YAML 1.1 coercing the GitHub ``on`` key to boolean."""
    try:
        yaml = importlib.import_module("yaml")
    except ImportError as exc:
        raise ReleaseValidationError("PyYAML is required to validate the parsed engine workflow") from exc
    payload = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    if not isinstance(payload, dict):
        raise ReleaseValidationError("engine workflow must parse as a mapping")
    return payload


def _validate_mandatory_workflow_step(
    step: dict[str, Any],
    *,
    label: str,
    expected_run: str,
    expected_env: dict[str, str] | None = None,
    expected_shell: str | None = None,
) -> None:
    if "if" in step or "continue-on-error" in step:
        raise ReleaseValidationError(f"mandatory {label} gate must be unconditional and fail closed")
    actual_run = step.get("run")
    if not isinstance(actual_run, str) or actual_run.rstrip("\n") != expected_run:
        raise ReleaseValidationError(f"mandatory {label} gate command is disabled or drifted")
    if expected_env is not None and step.get("env") != expected_env:
        raise ReleaseValidationError(f"mandatory {label} gate environment is invalid")
    if expected_shell is not None and step.get("shell") != expected_shell:
        raise ReleaseValidationError(f"mandatory {label} gate shell is invalid")


def _workflow_jobs_for_event(
    jobs: dict[str, Any],
    *,
    event_name: str,
    publish_release: bool,
    macos_pilot: bool,
    source_ref: str,
) -> set[str]:
    """Evaluate the deliberately small job-condition language used by the engine workflow."""
    enabled: set[str] = set()
    condition_values = {
        PUBLISH_JOB_CONDITION: (
            event_name == "workflow_dispatch" and publish_release and source_ref.startswith("refs/tags/v")
        ),
        MACOS_PILOT_CONDITION: event_name == "workflow_dispatch" and macos_pilot and not publish_release,
    }
    for name, raw_job in jobs.items():
        if not isinstance(raw_job, dict):
            raise ReleaseValidationError(f"engine workflow job {name!r} must be a mapping")
        condition = raw_job.get("if")
        if condition is None:
            enabled.add(name)
        elif condition in condition_values:
            if condition_values[condition]:
                enabled.add(name)
        else:
            raise ReleaseValidationError(f"engine workflow job {name!r} has an unmodeled event condition")
    return enabled


def validate_workflow_event_contract(workflow_path: Path) -> None:
    """Execute the manual-dispatch matrix and release-mutation boundary contract.

    The evaluator intentionally accepts only the two canonical job conditions. Any
    new trigger expression therefore requires an explicit review and test update.
    """
    workflow = _load_workflow(workflow_path)
    triggers = workflow.get("on")
    if not isinstance(triggers, dict) or set(triggers) != {"workflow_dispatch"}:
        raise ReleaseValidationError("engine distribution workflow must be manual dispatch only")
    concurrency = workflow.get("concurrency")
    # The group must split publish from non-publish runs: with one shared
    # ref-scoped group, a newly dispatched non-publish run (cancel-in-progress
    # true) would cancel an in-flight publication on the same ref.
    expected_concurrency = {
        "group": "engine-distribution-${{ inputs.publish_release && 'publish' || 'nonpublish' }}-${{ github.ref }}",
        "cancel-in-progress": "${{ !inputs.publish_release }}",
    }
    if concurrency != expected_concurrency:
        raise ReleaseValidationError(
            "engine distribution concurrency must cancel superseded non-publication runs without canceling publication"
        )
    dispatch = triggers["workflow_dispatch"]
    inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
    expected_defaults = {
        "measure_only": "true",
        "dev_mode": "false",
        "publish_release": "false",
        "macos_pilot": "false",
    }
    if not isinstance(inputs, dict) or set(inputs) != set(expected_defaults):
        raise ReleaseValidationError("engine dispatch input set drifted")
    for name, default in expected_defaults.items():
        spec = inputs[name]
        if (
            not isinstance(spec, dict)
            or spec.get("required") != "true"
            or spec.get("type") != "boolean"
            or spec.get("default") != default
        ):
            raise ReleaseValidationError(f"engine dispatch input contract drifted for {name}")

    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        raise ReleaseValidationError("engine workflow jobs must be a mapping")
    if not PUBLISH_ONLY_JOBS.issubset(jobs):
        raise ReleaseValidationError("engine publish job set is incomplete")
    for name in PUBLISH_ONLY_JOBS:
        if jobs[name].get("if") != PUBLISH_JOB_CONDITION:
            raise ReleaseValidationError(f"release job {name!r} is not publish-dispatch-only")
    macos_job = jobs.get("macos-pilot")
    if not isinstance(macos_job, dict) or macos_job.get("if") != MACOS_PILOT_CONDITION:
        raise ReleaseValidationError("macOS pilot is not excluded from the publish path")

    build_job = jobs.get("build")
    matrix = build_job.get("strategy", {}).get("matrix", {}) if isinstance(build_job, dict) else {}
    legs = matrix.get("include") if isinstance(matrix, dict) else None
    parsed_legs = legs if isinstance(legs, list) else []
    expected_matrix = {
        ("windows", "cpu"): ("cpu", "cpu"),
        ("windows", "cuda"): ("cuda", "cpu,cuda"),
        ("linux", "cpu"): ("cpu", "cpu"),
        ("linux", "cuda"): ("cuda", "cpu,cuda"),
    }
    observed_matrix: dict[tuple[Any, Any], tuple[Any, Any]] = {}
    observed_matrix = {
        (leg.get("platform"), leg.get("accel")): (
            leg.get("feature"),
            leg.get("test_feature"),
        )
        for leg in parsed_legs
        if isinstance(leg, dict)
    }
    if observed_matrix != expected_matrix:
        raise ReleaseValidationError(
            "engine release build matrix must contain exactly four Windows/Linux legs "
            "with CPU-capable native test features"
        )
    if any("macos" in str(value).lower() for leg in parsed_legs if isinstance(leg, dict) for value in leg.values()):
        raise ReleaseValidationError("macOS must not appear in the engine release build matrix")

    build_steps = build_job.get("steps") if isinstance(build_job, dict) else None
    if not isinstance(build_steps, list):
        raise ReleaseValidationError("engine build steps are missing")
    tag_guard = next(
        (
            step
            for step in build_steps
            if isinstance(step, dict) and step.get("name") == "Assert publish ref and tag match engine version"
        ),
        None,
    )
    tag_markers = (
        'version = tomllib.loads(Path("crates/amw-engine/Cargo.toml")',
        'expected = f"v{version}"',
        'os.environ["RELEASE_TAG"] != expected',
        'os.environ["RELEASE_REF"] != f"refs/tags/{expected}"',
    )
    if (
        not isinstance(tag_guard, dict)
        or tag_guard.get("if") != PUBLISH_JOB_CONDITION
        or any(marker not in str(tag_guard.get("run", "")) for marker in tag_markers)
    ):
        raise ReleaseValidationError("publish dispatch does not fail closed on immutable engine tag identity")

    mutation_markers = (
        "azure/trusted-signing-action@",
        "actions/attest-sbom@",
        "https://uploads.github.com/repos/",
        "gh api --method PATCH",
        "gh release create",
        "gh release edit",
        "gh release upload",
    )
    for name, raw_job in jobs.items():
        if not isinstance(raw_job, dict):
            continue
        serialized_steps = json.dumps(raw_job.get("steps", []), sort_keys=True)
        permissions = raw_job.get("permissions", {})
        mutates_release = any(marker in serialized_steps for marker in mutation_markers) or (
            isinstance(permissions, dict) and any(value == "write" for value in permissions.values())
        )
        if mutates_release and (name not in PUBLISH_ONLY_JOBS or raw_job.get("if") != PUBLISH_JOB_CONDITION):
            raise ReleaseValidationError(f"release mutation in job {name!r} is not publish-dispatch-only")
        needs = raw_job.get("needs", [])
        needs_list = [needs] if isinstance(needs, str) else needs
        if name in PUBLISH_ONLY_JOBS and "macos-pilot" in needs_list:
            raise ReleaseValidationError("macOS pilot must not feed the release dependency graph")

    base_jobs = {"supply-chain", "build"}
    cases = (
        ("workflow_dispatch", False, False, "refs/heads/main", base_jobs),
        ("workflow_dispatch", False, True, "refs/heads/main", base_jobs | {"macos-pilot"}),
        ("workflow_dispatch", True, False, "refs/tags/v1.2.3", base_jobs | set(PUBLISH_ONLY_JOBS)),
        ("workflow_dispatch", True, True, "refs/tags/v1.2.3", base_jobs | set(PUBLISH_ONLY_JOBS)),
        ("workflow_dispatch", True, False, "refs/heads/main", base_jobs),
    )
    for event_name, publish_release, macos_pilot, source_ref, expected_jobs in cases:
        observed = _workflow_jobs_for_event(
            jobs,
            event_name=event_name,
            publish_release=publish_release,
            macos_pilot=macos_pilot,
            source_ref=source_ref,
        )
        if observed != expected_jobs:
            raise ReleaseValidationError(
                f"engine parsed event matrix drifted for {event_name}, publish={publish_release}, macos={macos_pilot}"
            )


def validate_workflow_supply_chain_bindings(workflow_path: Path, ci_requirements_path: Path) -> None:
    """Require one hash-locked, binary-only CI verifier environment."""
    workflow = _load_workflow(workflow_path)
    jobs = workflow.get("jobs")
    supply_chain = jobs.get("supply-chain") if isinstance(jobs, dict) else None
    steps = supply_chain.get("steps") if isinstance(supply_chain, dict) else None
    if not isinstance(steps, list):
        raise ReleaseValidationError("engine supply-chain steps are missing")

    setup_python = next(
        (
            step
            for step in steps
            if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/setup-python@")
        ),
        None,
    )
    if (
        not isinstance(setup_python, dict)
        or setup_python.get("with", {}).get("cache-dependency-path") != "requirements-engine-ci.txt"
    ):
        raise ReleaseValidationError("engine CI cache is not bound to the hash-locked verifier requirements")
    all_runs = "\n".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))
    expected_install = (
        "python -m pip install --disable-pip-version-check --require-hashes "
        "--only-binary=:all: --requirement requirements-engine-ci.txt"
    )
    if all_runs.count("python -m pip install") != 1 or expected_install not in all_runs:
        raise ReleaseValidationError("engine CI verifier dependencies must use one hash-locked binary-only install")
    verifier_step = next(
        (
            step
            for step in steps
            if isinstance(step, dict)
            and step.get("name") == "Exercise production Sigstore verifier against official GitHub fixture"
        ),
        None,
    )
    if not isinstance(verifier_step, dict):
        raise ReleaseValidationError("Sigstore verifier step must consume the preinstalled hash-locked environment")
    install_index = next(
        (
            index
            for index, step in enumerate(steps)
            if isinstance(step, dict) and step.get("name") == "Install pinned supply-chain tools"
        ),
        -1,
    )
    verifier_index = steps.index(verifier_step)
    if install_index < 0 or install_index >= verifier_index:
        raise ReleaseValidationError("Sigstore verifier must run after the hash-locked environment is installed")
    _validate_mandatory_workflow_step(
        verifier_step,
        label="Sigstore verifier",
        expected_run=(
            "python -m pytest tests/test_engine_provenance_attestation.py "
            "-k official_github_docs_attestation -q --noconftest"
        ),
        expected_env={"AMW_TEST_SIGSTORE_REFRESH": "1"},
    )
    validation_step = next(
        (
            step
            for step in steps
            if isinstance(step, dict) and step.get("name") == "Validate exact dependency and attribution evidence"
        ),
        None,
    )
    if not isinstance(validation_step, dict):
        raise ReleaseValidationError("engine CI requirements freshness and policy checks are not wired")
    _validate_mandatory_workflow_step(
        validation_step,
        label="dependency freshness, SBOM, license, and release policy",
        expected_run=(
            "uv pip compile requirements-engine-ci.in \\\n"
            '  --output-file "${RUNNER_TEMP}/requirements-engine-ci.txt" \\\n'
            "  --generate-hashes \\\n"
            "  --only-binary :all: \\\n"
            "  --python-platform linux \\\n"
            "  --python-version 3.12 \\\n"
            "  --no-header\n"
            'cmp requirements-engine-ci.txt "${RUNNER_TEMP}/requirements-engine-ci.txt"\n'
            "python scripts/check_converter_lock.py\n"
            "python scripts/generate_spdx_sbom.py --check\n"
            "python scripts/check_dependency_license_export.py --strict --all-extras\n"
            "python scripts/generate_engine_attribution.py --check\n"
            "python scripts/validate_engine_release.py policy \\\n"
            "  --workflow .github/workflows/engine.yml \\\n"
            "  --ci-requirements requirements-engine-ci.txt \\\n"
            "  --ledger docs/reference/engine-size-baselines.json \\\n"
            "  --cuda-evidence docs/reference/engine-cuda-certification.json"
        ),
    )
    validation_index = steps.index(validation_step)
    if not install_index < validation_index < verifier_index:
        raise ReleaseValidationError("dependency evidence gates must run after install and before Sigstore")

    lines = ci_requirements_path.read_text(encoding="utf-8").splitlines()
    requirements: dict[str, tuple[str, int]] = {}
    current_name: str | None = None
    current_version = ""
    current_hashes = 0

    def finish_requirement() -> None:
        nonlocal current_name, current_version, current_hashes
        if current_name is None:
            return
        if current_hashes == 0:
            raise ReleaseValidationError(f"CI requirement {current_name!r} has no SHA-256 hashes")
        if current_name in requirements:
            raise ReleaseValidationError(f"CI requirement {current_name!r} is duplicated")
        requirements[current_name] = (current_version, current_hashes)
        current_name = None
        current_version = ""
        current_hashes = 0

    for line in lines:
        if line and not line[0].isspace() and not line.startswith("#"):
            finish_requirement()
            requirement = line.removesuffix("\\").strip()
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;\\]+)", requirement)
            if match is None:
                raise ReleaseValidationError("CI requirements must contain only exact version pins")
            current_name = match.group(1).lower().replace("_", "-").replace(".", "-")
            current_version = match.group(2)
        elif "--hash=" in line:
            if current_name is None or re.fullmatch(r"\s+--hash=sha256:[0-9a-f]{64}(?: \\)?", line) is None:
                raise ReleaseValidationError("CI requirements contain a malformed or non-SHA-256 hash")
            current_hashes += 1
    finish_requirement()
    missing = CI_VERIFIER_REQUIREMENTS - requirements.keys()
    if missing:
        raise ReleaseValidationError(f"CI verifier lock is missing: {', '.join(sorted(missing))}")


def validate_workflow_native_fixture_bindings(workflow_path: Path) -> None:
    """Require both governed GGUF fixtures before native engine tests execute."""
    workflow = _load_workflow(workflow_path)
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        raise ReleaseValidationError("engine workflow jobs must be a mapping")
    build = jobs.get("build")
    build_steps = build.get("steps") if isinstance(build, dict) else None
    supply_chain = jobs.get("supply-chain")
    supply_steps = supply_chain.get("steps") if isinstance(supply_chain, dict) else None
    if not isinstance(build_steps, list) or not isinstance(supply_steps, list):
        raise ReleaseValidationError("engine workflow fixture consumer jobs are incomplete")

    def named_step(steps: list[Any], name: str) -> tuple[int, dict[str, Any]]:
        for index, step in enumerate(steps):
            if isinstance(step, dict) and step.get("name") == name:
                return index, step
        raise ReleaseValidationError(f"engine workflow is missing fixture step {name!r}")

    native_index, native_step = named_step(build_steps, "Provision governed native GGUF fixture")
    fim_index, fim_step = named_step(build_steps, "Provision governed native FIM GGUF fixture")
    test_index, test_step = named_step(build_steps, "Run native engine tests with governed fixture")
    install_index, install_step = named_step(build_steps, "Install hash-locked real-process test environment")
    server_index, server_step = named_step(build_steps, "Build native real-process test server")
    embeddings_index, embeddings_step = named_step(
        build_steps,
        "Prove public native embeddings through the real server",
    )
    public_fim_index, public_fim_step = named_step(
        build_steps,
        "Prove public native FIM through the real server",
    )
    setup_python = next(
        (
            (index, step)
            for index, step in enumerate(build_steps)
            if isinstance(step, dict)
            and step.get("uses") == "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
        ),
        None,
    )
    if (
        setup_python is None
        or setup_python[1].get("with")
        != {
            "python-version": "3.12",
            "cache": "pip",
            "cache-dependency-path": "requirements-engine-ci.txt",
        }
        or "if" in setup_python[1]
        or "continue-on-error" in setup_python[1]
    ):
        raise ReleaseValidationError("real-process test Python environment is not pinned and fail closed")
    if not (
        setup_python[0]
        < install_index
        < native_index
        < fim_index
        < test_index
        < server_index
        < embeddings_index
        < public_fim_index
    ):
        raise ReleaseValidationError("governed fixtures and public real-process tests are out of order")

    native_path = "${{ runner.temp }}/amw-engine-native-fixture/tinyllama-15M-stories-Q2_K.gguf"
    fim_path = "${{ runner.temp }}/amw-engine-native-fim-fixture/qwen2.5-coder-0.5b-instruct-q2_k.gguf"
    _validate_mandatory_workflow_step(
        install_step,
        label="real-process Python dependency install",
        expected_run=(
            "python -m pip install --disable-pip-version-check --require-hashes "
            "--only-binary=:all: --requirement requirements-engine-ci.txt"
        ),
    )
    _validate_mandatory_workflow_step(
        native_step,
        label="native fixture provisioning",
        expected_run=(
            "python scripts/verify_engine_model_fixture.py \\\n"
            '  --model-output "$AMW_ENGINE_NATIVE_TEST_MODEL" \\\n'
            '  --evidence-directory "${{ runner.temp }}/amw-engine-native-fixture/evidence"'
        ),
        expected_env={"AMW_ENGINE_NATIVE_TEST_MODEL": native_path},
        expected_shell="bash",
    )
    _validate_mandatory_workflow_step(
        fim_step,
        label="native FIM fixture provisioning",
        expected_run=(
            "python scripts/verify_engine_model_fixture.py \\\n"
            "  --fixture fim \\\n"
            '  --model-output "$AMW_ENGINE_NATIVE_FIM_TEST_MODEL" \\\n'
            '  --evidence-directory "${{ runner.temp }}/amw-engine-native-fim-fixture/evidence"'
        ),
        expected_env={"AMW_ENGINE_NATIVE_FIM_TEST_MODEL": fim_path},
        expected_shell="bash",
    )
    _validate_mandatory_workflow_step(
        test_step,
        label="native engine test",
        expected_run='cargo test -p amw-engine --features "${{ matrix.test_feature }}" --locked',
        expected_env={
            "AMW_ENGINE_NATIVE_TEST_MODEL": native_path,
            "AMW_ENGINE_NATIVE_FIM_TEST_MODEL": fim_path,
        },
        expected_shell="bash",
    )
    shared_real_process_env = {
        "AMW_ENGINE_NATIVE_TEST_MODEL": native_path,
        "AMW_ENGINE_NATIVE_FIM_TEST_MODEL": fim_path,
    }
    _validate_mandatory_workflow_step(
        server_step,
        label="native real-process server build",
        expected_run=(
            'cargo build -p amw-engine --bin amw-engine-server --features "${{ matrix.test_feature }}" --locked'
        ),
        expected_shell="bash",
    )
    _validate_mandatory_workflow_step(
        embeddings_step,
        label="public native embeddings real-process test",
        expected_run=(
            "python -m pytest tests/test_engine_embeddings_contract.py::"
            "test_governed_fixture_returns_stable_normalized_native_embeddings -q -n 0 --noconftest"
        ),
        expected_env=shared_real_process_env,
        expected_shell="bash",
    )
    _validate_mandatory_workflow_step(
        public_fim_step,
        label="public native FIM real-process test",
        expected_run=(
            "python -m pytest tests/test_engine_contract.py::"
            "test_governed_fim_model_completes_public_infill_and_stops_at_exact_limit -q -n 0 --noconftest"
        ),
        expected_env=shared_real_process_env,
        expected_shell="bash",
    )

    _, evidence_step = named_step(supply_steps, "Verify and retain commit-pinned model license evidence")
    _validate_mandatory_workflow_step(
        evidence_step,
        label="fixture license evidence",
        expected_run=(
            "python scripts/verify_engine_model_fixture.py \\\n"
            '  --model-output "${{ runner.temp }}/supply-chain-fixture.gguf" \\\n'
            "  --evidence-directory model-license-evidence\n"
            "python scripts/verify_engine_model_fixture.py \\\n"
            "  --fixture fim \\\n"
            '  --model-output "${{ runner.temp }}/supply-chain-fim-fixture.gguf" \\\n'
            "  --evidence-directory model-license-evidence/fim"
        ),
        expected_shell="bash",
    )
    workflow_text = workflow_path.read_text(encoding="utf-8")
    fim_provenance_markers = (
        f'"fim_fixture_model_url": "{FIM_MODEL_URL}"',
        f'"fim_fixture_model_revision": "{FIM_MODEL_REVISION}"',
        f'"fim_fixture_model_sha256": "{FIM_MODEL_SHA256}"',
        f'"fim_fixture_model_size_bytes": {FIM_MODEL_SIZE}',
        f'"fim_fixture_license_url": "{FIM_LICENSE_URL}"',
        f'"fim_fixture_license_sha256": "{FIM_LICENSE_SHA256}"',
    )
    if any(marker not in workflow_text for marker in fim_provenance_markers):
        raise ReleaseValidationError("release rebuild provenance omits the governed FIM fixture identity")


def _validate_cuda_evidence(
    path: Path,
    *,
    expected_commit: str | None = None,
    expected_workflow_run_url: str | None = None,
    expected_workflow_run_attempt: int | None = None,
    expected_runner_id: int | None = None,
    expected_runner_name: str | None = None,
    expected_machine_id_sha256: str | None = None,
    expected_service_account: str | None = None,
    expected_work_directory: str | None = None,
    certifier_public_key_der_base64: str | None = None,
    require_certified: bool = False,
) -> None:
    evidence = _read_json_object(path, "CUDA certification evidence")
    base_fields = {
        "schema_version",
        "status",
        "source_commit",
        "workflow_run_url",
        "runner_labels",
        "device",
        "offload",
    }
    schema_version = evidence.get("schema_version")
    required_by_schema = {
        1: base_fields,
        2: base_fields | {"workflow_run_attempt", "subject", "host_attestation", "certifier_signature"},
        3: base_fields
        | {"workflow_run_attempt", "subject", "host_attestation", "certifier_signature", "runner_identity"},
    }
    required = required_by_schema.get(schema_version) if type(schema_version) is int else None
    if required is None or set(evidence) != required:
        raise ReleaseValidationError("CUDA certification evidence fields or schema version are invalid")
    device = evidence["device"]
    offload = evidence["offload"]
    if not isinstance(device, dict) or set(device) != {"name", "uuid", "driver_version"}:
        raise ReleaseValidationError("CUDA certification device assertion is invalid")
    if not isinstance(offload, dict) or set(offload) != {"asserted", "model_sha256", "device_memory_delta_bytes"}:
        raise ReleaseValidationError("CUDA certification offload assertion is invalid")
    if evidence["status"] == "blocked":
        if (
            evidence["source_commit"] is not None
            or evidence["workflow_run_url"] is not None
            or evidence["runner_labels"] != []
            or any(value is not None for value in device.values())
            or offload != {"asserted": False, "model_sha256": None, "device_memory_delta_bytes": None}
        ):
            raise ReleaseValidationError("blocked CUDA evidence must not contain partial certification claims")
        if require_certified:
            raise ReleaseValidationError("CUDA publication requires governed GPU/device/offload certification")
        return
    if evidence["status"] != "certified":
        raise ReleaseValidationError("CUDA certification status must be blocked or certified")
    if schema_version == 1:
        raise ReleaseValidationError("certified CUDA evidence must include governed host isolation attestation")
    source_commit = evidence["source_commit"]
    if not isinstance(source_commit, str) or COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ReleaseValidationError("CUDA certification source commit is invalid")
    if expected_commit is not None and source_commit != expected_commit:
        raise ReleaseValidationError("CUDA certification does not match the publication source commit")
    run_url = evidence["workflow_run_url"]
    labels = evidence["runner_labels"]
    if not isinstance(run_url, str) or not re.fullmatch(r"https://github\.com/.+/actions/runs/\d+", run_url):
        raise ReleaseValidationError("CUDA certification must name an immutable Actions run")
    if expected_workflow_run_url is not None and run_url != expected_workflow_run_url:
        raise ReleaseValidationError("CUDA certification does not come from the current publication workflow run")
    run_attempt = evidence["workflow_run_attempt"]
    if (
        not isinstance(run_attempt, int)
        or isinstance(run_attempt, bool)
        or run_attempt <= 0
        or (expected_workflow_run_attempt is not None and run_attempt != expected_workflow_run_attempt)
    ):
        raise ReleaseValidationError("CUDA certification does not match the current workflow run attempt")
    if labels != list(CUDA_CERTIFICATION_LABELS):
        raise ReleaseValidationError("CUDA certification must record the exact governed GPU runner labels")
    _validate_cuda_certification_subject(evidence["subject"])
    _validate_cuda_certifier_signature(
        evidence,
        certifier_public_key_der_base64=certifier_public_key_der_base64,
    )
    host_attestation = evidence["host_attestation"]
    if (
        not isinstance(host_attestation, dict)
        or set(host_attestation)
        != {
            "machine_id_sha256",
            "service_account",
            "work_directory",
            "runner_service_count",
            "training_runner_present",
        }
        or not isinstance(host_attestation["machine_id_sha256"], str)
        or SHA256_PATTERN.fullmatch(host_attestation["machine_id_sha256"]) is None
        or not isinstance(host_attestation["service_account"], str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", host_attestation["service_account"]) is None
        or not isinstance(host_attestation["work_directory"], str)
        or not PurePosixPath(host_attestation["work_directory"]).is_absolute()
        or ".." in PurePosixPath(host_attestation["work_directory"]).parts
        or not isinstance(host_attestation["runner_service_count"], int)
        or isinstance(host_attestation["runner_service_count"], bool)
        or host_attestation["runner_service_count"] != 1
        or host_attestation["training_runner_present"] is not False
    ):
        raise ReleaseValidationError(
            "CUDA certification host attestation does not prove one isolated non-training runner service"
        )
    expected_host = (
        expected_machine_id_sha256,
        expected_service_account,
        expected_work_directory,
    )
    if any(value is not None for value in expected_host) and any(value is None for value in expected_host):
        raise ReleaseValidationError("protected CUDA host authority must be supplied as one complete tuple")
    if expected_machine_id_sha256 is not None and (
        host_attestation["machine_id_sha256"] != expected_machine_id_sha256
        or host_attestation["service_account"] != expected_service_account
        or host_attestation["work_directory"] != expected_work_directory
    ):
        raise ReleaseValidationError("CUDA certification host identity does not match protected authority")
    expected_runner = (expected_runner_id, expected_runner_name)
    if any(value is not None for value in expected_runner) and any(value is None for value in expected_runner):
        raise ReleaseValidationError("protected CUDA runner authority must include both ID and name")
    if schema_version != 3:
        if require_certified:
            raise ReleaseValidationError(
                "CUDA certification is not bound to the protected workflow-job and repository runner identity"
            )
    else:
        runner_identity = evidence["runner_identity"]
        if (
            not isinstance(runner_identity, dict)
            or set(runner_identity) != {"id", "name", "labels"}
            or not isinstance(runner_identity["id"], int)
            or isinstance(runner_identity["id"], bool)
            or runner_identity["id"] <= 0
            or not isinstance(runner_identity["name"], str)
            or not runner_identity["name"]
            or runner_identity["labels"] != list(CUDA_CERTIFICATION_LABELS)
            or CUDA_TRAINING_RUNNER_LABEL in runner_identity["labels"]
        ):
            raise ReleaseValidationError("CUDA certification runner identity is invalid or training-eligible")
        if expected_runner_id is not None and (
            runner_identity["id"] != expected_runner_id or runner_identity["name"] != expected_runner_name
        ):
            raise ReleaseValidationError("CUDA certification runner identity does not match protected authority")
    if any(not isinstance(value, str) or not value for value in device.values()):
        raise ReleaseValidationError("CUDA certification must assert a concrete device identity")
    if (
        offload["asserted"] is not True
        or not isinstance(offload["model_sha256"], str)
        or offload["model_sha256"] != CUDA_FIXTURE_SHA256
        or not isinstance(offload["device_memory_delta_bytes"], int)
        or isinstance(offload["device_memory_delta_bytes"], bool)
        or offload["device_memory_delta_bytes"] <= 0
    ):
        raise ReleaseValidationError("CUDA certification does not prove model offload on the asserted device")


def _cuda_certifier_signature_payload(evidence: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in evidence.items() if key not in {"certifier_signature", "runner_identity"}}
    payload["schema_version"] = 2
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _validate_cuda_certification_subject(
    subject: Any,
    *,
    subject_directory: Path | None = None,
    require_manifest: bool = False,
) -> None:
    expected_names = {
        "archive_name": "amw-engine-linux-cuda.zip",
        "manifest_name": "manifest-linux-cuda.json",
    }
    if (
        not isinstance(subject, dict)
        or set(subject)
        != {
            "archive_name",
            "archive_sha256",
            "archive_size_bytes",
            "manifest_name",
            "manifest_sha256",
            "manifest_size_bytes",
        }
        or any(subject.get(key) != value for key, value in expected_names.items())
        or any(
            not isinstance(subject.get(key), str) or SHA256_PATTERN.fullmatch(subject[key]) is None
            for key in ("archive_sha256", "manifest_sha256")
        )
        or any(
            not isinstance(subject.get(key), int) or isinstance(subject[key], bool) or subject[key] <= 0
            for key in ("archive_size_bytes", "manifest_size_bytes")
        )
    ):
        raise ReleaseValidationError("CUDA certification subject identity is invalid")
    if subject_directory is None:
        return
    archive = subject_directory / subject["archive_name"]
    manifest_path = subject_directory / subject["manifest_name"]
    if (
        not archive.is_file()
        or archive.stat().st_size != subject["archive_size_bytes"]
        or sha256_file(archive) != subject["archive_sha256"]
    ):
        raise ReleaseValidationError("CUDA certification subject does not match the same-run Linux CUDA archive")
    if not manifest_path.is_file():
        if require_manifest:
            raise ReleaseValidationError("CUDA certification subject manifest is unavailable")
        return
    if (
        manifest_path.stat().st_size != subject["manifest_size_bytes"]
        or sha256_file(manifest_path) != subject["manifest_sha256"]
    ):
        raise ReleaseValidationError("CUDA certification subject does not match the same-run Linux CUDA manifest")
    manifest = _read_json_object(manifest_path, "Linux CUDA release fragment")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1 or not isinstance(artifacts[0], dict):
        raise ReleaseValidationError("Linux CUDA release fragment does not identify one archive")
    artifact = artifacts[0]
    if (
        artifact.get("platform") != "linux"
        or artifact.get("accel") != "cuda"
        or artifact.get("file") != subject["archive_name"]
        or artifact.get("sha256") != subject["archive_sha256"]
        or artifact.get("size_bytes") != subject["archive_size_bytes"]
    ):
        raise ReleaseValidationError("Linux CUDA release fragment does not bind the certified archive")


def _validate_cuda_certifier_signature(
    evidence: dict[str, Any],
    *,
    certifier_public_key_der_base64: str | None,
) -> None:
    signature = evidence["certifier_signature"]
    if (
        not isinstance(signature, dict)
        or set(signature) != {"algorithm", "public_key_sha256", "value_base64"}
        or signature["algorithm"] != "ecdsa-p256-sha256"
        or not isinstance(signature["public_key_sha256"], str)
        or SHA256_PATTERN.fullmatch(signature["public_key_sha256"]) is None
        or not isinstance(signature["value_base64"], str)
        or not signature["value_base64"]
    ):
        raise ReleaseValidationError("CUDA certifier signature metadata is invalid")
    try:
        signature_bytes = base64.b64decode(signature["value_base64"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReleaseValidationError("CUDA certifier signature is not canonical base64") from exc
    if not 64 <= len(signature_bytes) <= 80:
        raise ReleaseValidationError("CUDA certifier ECDSA signature length is invalid")
    if certifier_public_key_der_base64 is None:
        return
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as exc:
        raise ReleaseValidationError("CUDA signature verification dependency is unavailable") from exc
    try:
        public_key_der = base64.b64decode(certifier_public_key_der_base64, validate=True)
        public_key = serialization.load_der_public_key(public_key_der)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ReleaseValidationError("protected CUDA certifier public key is invalid") from exc
    if (
        not isinstance(public_key, ec.EllipticCurvePublicKey)
        or not isinstance(public_key.curve, ec.SECP256R1)
        or hashlib.sha256(public_key_der).hexdigest() != signature["public_key_sha256"]
    ):
        raise ReleaseValidationError("CUDA certifier signature key does not match protected authority")
    try:
        public_key.verify(
            signature_bytes,
            _cuda_certifier_signature_payload(evidence),
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature as exc:
        raise ReleaseValidationError(
            "CUDA certifier signature does not authenticate the raw certification evidence"
        ) from exc


def bind_cuda_runner_inventory(
    cuda_evidence_path: Path,
    runner_metadata_path: Path,
    workflow_jobs_path: Path,
    subject_directory: Path,
    output_path: Path,
    *,
    expected_runner_id: int,
    expected_runner_name: str,
    expected_machine_id_sha256: str,
    expected_service_account: str,
    expected_work_directory: str,
    certifier_public_key_der_base64: str,
    expected_commit: str,
    expected_workflow_run_url: str,
    expected_workflow_run_attempt: int,
) -> dict[str, Any]:
    """Bind device evidence to protected identity using independent GitHub metadata."""
    if (
        not isinstance(expected_runner_id, int)
        or isinstance(expected_runner_id, bool)
        or expected_runner_id <= 0
        or not expected_runner_name
    ):
        raise ReleaseValidationError("protected CUDA runner ID and name are invalid")
    _validate_cuda_evidence(
        cuda_evidence_path,
        expected_commit=expected_commit,
        expected_workflow_run_url=expected_workflow_run_url,
        expected_workflow_run_attempt=expected_workflow_run_attempt,
        expected_machine_id_sha256=expected_machine_id_sha256,
        expected_service_account=expected_service_account,
        expected_work_directory=expected_work_directory,
        certifier_public_key_der_base64=certifier_public_key_der_base64,
    )
    raw_evidence = _read_json_object(cuda_evidence_path, "raw CUDA certification evidence")
    if raw_evidence.get("schema_version") != 2 or raw_evidence.get("status") != "certified":
        raise ReleaseValidationError("raw CUDA certification must be an unbound certified schema version 2 result")
    _validate_cuda_certification_subject(
        raw_evidence["subject"],
        subject_directory=subject_directory,
        require_manifest=True,
    )

    workflow_metadata = _read_json_object(workflow_jobs_path, "workflow job inventory")
    jobs = workflow_metadata.get("jobs")
    job_count = workflow_metadata.get("total_count")
    if (
        set(workflow_metadata) != {"total_count", "jobs"}
        or not isinstance(job_count, int)
        or isinstance(job_count, bool)
        or job_count < 0
        or job_count > 100
        or not isinstance(jobs, list)
        or job_count != len(jobs)
    ):
        raise ReleaseValidationError("workflow job inventory is incomplete or malformed")
    run_match = re.fullmatch(r"https://github\.com/[^/]+/[^/]+/actions/runs/(\d+)", expected_workflow_run_url)
    if run_match is None:
        raise ReleaseValidationError("expected CUDA workflow run URL is invalid")
    run_id = int(run_match.group(1))
    job_matches = [
        job
        for job in jobs
        if isinstance(job, dict) and job.get("name") == CUDA_CERTIFICATION_JOB_NAME and job.get("run_id") == run_id
    ]
    if len(job_matches) != 1:
        raise ReleaseValidationError("CUDA certification workflow job is absent or ambiguous")
    workflow_job = job_matches[0]
    workflow_labels = workflow_job.get("labels")
    if (
        workflow_job.get("status") != "completed"
        or workflow_job.get("conclusion") != "success"
        or workflow_job.get("runner_id") != expected_runner_id
        or workflow_job.get("runner_name") != expected_runner_name
        or not isinstance(workflow_labels, list)
        or any(not isinstance(label, str) for label in workflow_labels)
        or len(workflow_labels) != len(CUDA_CERTIFICATION_LABELS)
        or set(workflow_labels) != set(CUDA_CERTIFICATION_LABELS)
    ):
        raise ReleaseValidationError("CUDA workflow job does not match the protected successful runner identity")

    metadata = _read_json_object(runner_metadata_path, "repository runner inventory")
    runners = metadata.get("runners")
    total_count = metadata.get("total_count")
    if (
        set(metadata) != {"total_count", "runners"}
        or not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count < 0
        or total_count > 100
        or not isinstance(runners, list)
        or total_count != len(runners)
    ):
        raise ReleaseValidationError("repository runner inventory is incomplete or malformed")
    matches = [
        runner
        for runner in runners
        if isinstance(runner, dict)
        and runner.get("id") == expected_runner_id
        and runner.get("name") == expected_runner_name
    ]
    if len(matches) != 1:
        raise ReleaseValidationError("protected CUDA runner is absent or ambiguous in repository inventory")
    runner = matches[0]
    labels = runner.get("labels")
    if (
        not isinstance(runner.get("id"), int)
        or isinstance(runner["id"], bool)
        or runner["id"] <= 0
        or runner.get("os") != "linux"
        or runner.get("status") != "online"
        or not isinstance(runner.get("busy"), bool)
        or not isinstance(labels, list)
    ):
        raise ReleaseValidationError("protected CUDA runner state is not online, Linux, and identity-complete")
    label_types: dict[str, str] = {}
    for label in labels:
        if (
            not isinstance(label, dict)
            or set(label) != {"id", "name", "type"}
            or not isinstance(label.get("id"), int)
            or isinstance(label["id"], bool)
            or label["id"] <= 0
            or not isinstance(label.get("name"), str)
            or not isinstance(label.get("type"), str)
        ):
            raise ReleaseValidationError("protected CUDA runner label inventory is malformed")
        normalized = label["name"].lower()
        if normalized in label_types:
            raise ReleaseValidationError("protected CUDA runner label inventory contains duplicates")
        label_types[normalized] = label["type"]
    expected_types = {
        "self-hosted": "read-only",
        "vetinari-engine-gpu": "custom",
        "linux": "read-only",
        "x64": "read-only",
        "cuda-12-4": "custom",
    }
    if label_types != expected_types or CUDA_TRAINING_RUNNER_LABEL in label_types:
        raise ReleaseValidationError("protected CUDA runner labels are not exact or include training eligibility")

    bound = {
        **raw_evidence,
        "schema_version": 3,
        "runner_identity": {
            "id": expected_runner_id,
            "name": expected_runner_name,
            "labels": list(CUDA_CERTIFICATION_LABELS),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(bound, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return bound


def validate_cuda_certification_result(
    path: Path,
    *,
    expected_commit: str,
    expected_workflow_run_url: str,
    expected_workflow_run_attempt: int | None = None,
    expected_runner_id: int | None = None,
    expected_runner_name: str | None = None,
    expected_machine_id_sha256: str | None = None,
    expected_service_account: str | None = None,
    expected_work_directory: str | None = None,
    certifier_public_key_der_base64: str | None = None,
) -> None:
    """Validate one protected same-run CUDA certification result.

    Args:
        path: JSON result emitted by the independently governed GPU verifier.
        expected_commit: Exact source commit of the release workflow run.
        expected_workflow_run_url: Immutable URL of that same workflow run.

    Raises:
        ReleaseValidationError: If the result is incomplete or not bound to the run.
    """
    _validate_cuda_evidence(
        path,
        expected_commit=expected_commit,
        expected_workflow_run_url=expected_workflow_run_url,
        expected_workflow_run_attempt=expected_workflow_run_attempt,
        expected_runner_id=expected_runner_id,
        expected_runner_name=expected_runner_name,
        expected_machine_id_sha256=expected_machine_id_sha256,
        expected_service_account=expected_service_account,
        expected_work_directory=expected_work_directory,
        certifier_public_key_der_base64=certifier_public_key_der_base64,
        require_certified=True,
    )


def validate_draft_release_assets(root: Path, metadata_path: Path) -> None:
    """Compare the exact local handoff with GitHub's draft-release asset metadata.

    Args:
        root: Flat directory containing the four archives and standalone manifest.
        metadata_path: JSON emitted by GitHub's release API for the still-draft release.

    Raises:
        ReleaseValidationError: If names, sizes, digests, or draft state differ.
    """
    expected_names = RELEASE_ASSET_NAMES
    actual_names = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual_names != expected_names or any(path.is_dir() for path in root.iterdir()):
        raise ReleaseValidationError(f"draft publisher input is not flat and exact: {sorted(actual_names)}")
    manifest = _read_json_object(root / "manifest.json", "draft release manifest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(EXPECTED_LEGS):
        raise ReleaseValidationError("draft release manifest does not contain four artifact rows")
    manifest_rows: dict[str, tuple[int, str]] = {}
    for row in artifacts:
        if not isinstance(row, dict) or set(row) != {"platform", "accel", "file", "sha256", "size_bytes"}:
            raise ReleaseValidationError("draft release manifest artifact fields are invalid")
        name = row["file"]
        if not isinstance(name, str):
            raise ReleaseValidationError("draft release manifest artifact filename is invalid")
        selector = EXPECTED_LEGS.get(name)
        if (
            selector is None
            or name in manifest_rows
            or (row["platform"], row["accel"]) != selector
            or not isinstance(row["size_bytes"], int)
            or isinstance(row["size_bytes"], bool)
            or row["size_bytes"] <= 0
            or not isinstance(row["sha256"], str)
            or SHA256_PATTERN.fullmatch(row["sha256"]) is None
        ):
            raise ReleaseValidationError(f"draft release manifest artifact identity is invalid: {name!r}")
        manifest_rows[name] = (row["size_bytes"], row["sha256"])
    local_rows = {name: ((root / name).stat().st_size, sha256_file(root / name)) for name in expected_names}
    if any(local_rows[name] != manifest_rows[name] for name in EXPECTED_LEGS):
        raise ReleaseValidationError("draft release manifest does not match the local archive bytes")
    _validate_manifest_cuda_evidence(manifest, root)
    _validate_consumer_release_authority(root, manifest)
    metadata = _read_json_object(metadata_path, "draft release asset metadata")
    if set(metadata) != {"isDraft", "assets"} or metadata["isDraft"] is not True:
        raise ReleaseValidationError("release is not still a draft immediately before publication")
    assets = metadata["assets"]
    if not isinstance(assets, list) or len(assets) != len(expected_names):
        raise ReleaseValidationError("draft release asset set is not exact")
    remote_rows: dict[str, tuple[int, str]] = {}
    for row in assets:
        if not isinstance(row, dict) or set(row) != {"name", "size", "digest"}:
            raise ReleaseValidationError("draft release asset metadata fields are invalid")
        name = row["name"]
        digest = row["digest"]
        if (
            not isinstance(name, str)
            or name in remote_rows
            or not isinstance(row["size"], int)
            or isinstance(row["size"], bool)
            or row["size"] <= 0
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or SHA256_PATTERN.fullmatch(digest.removeprefix("sha256:")) is None
        ):
            raise ReleaseValidationError(f"draft release asset identity is invalid: {name!r}")
        remote_rows[name] = (row["size"], digest.removeprefix("sha256:"))
    if set(remote_rows) != expected_names or remote_rows != local_rows:
        raise ReleaseValidationError("draft release asset names, sizes, or digests differ from the validated handoff")


def bind_draft_release(
    metadata_path: Path,
    output_path: Path,
    *,
    repository: str,
    expected_tag: str,
    expected_commit: str,
    expected_uploader: str,
    github_env_path: Path | None = None,
) -> dict[str, Any]:
    """Bind the publication critical window to one empty draft release ID.

    Args:
        metadata_path: Raw GitHub REST release object resolved from the expected tag.
        output_path: Destination for the immutable local binding record.
        repository: Exact ``owner/repository`` identity.
        expected_tag: Immutable release tag expected by the workflow.
        expected_commit: Peeled source commit already verified by the workflow.
        expected_uploader: Protected identity expected to own every uploaded asset.
        github_env_path: Optional GitHub environment file receiving ``RELEASE_ID``.

    Returns:
        The validated initial binding record.

    Raises:
        ReleaseValidationError: If release identity, draft state, or exclusivity inputs are invalid.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ReleaseValidationError("release binding repository identity is invalid")
    if not expected_tag or not re.fullmatch(r"[A-Za-z0-9._-]+", expected_tag):
        raise ReleaseValidationError("release binding tag identity is invalid")
    if COMMIT_PATTERN.fullmatch(expected_commit) is None:
        raise ReleaseValidationError("release binding source commit is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[bot\])?", expected_uploader):
        raise ReleaseValidationError("release binding uploader identity is invalid")
    metadata = _read_json_object(metadata_path, "draft release identity")
    release_id = metadata.get("id")
    if (
        not isinstance(release_id, int)
        or isinstance(release_id, bool)
        or release_id <= 0
        or metadata.get("tag_name") != expected_tag
        or metadata.get("draft") is not True
        or metadata.get("immutable") is not False
        or metadata.get("assets") != []
    ):
        raise ReleaseValidationError("release binding requires one exact empty mutable draft")
    binding: dict[str, Any] = {
        "schema_version": 1,
        "repository": repository,
        "release_id": release_id,
        "tag_name": expected_tag,
        "source_commit": expected_commit,
        "expected_uploader": expected_uploader,
        "assets": [],
    }
    _write_release_binding(output_path, binding)
    if github_env_path is not None:
        with github_env_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"RELEASE_ID={release_id}\n")
    return binding


def capture_draft_release_assets(
    root: Path,
    metadata_path: Path,
    binding_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Capture exact uploaded asset IDs into the bound draft-release record.

    Args:
        root: Flat validated publisher handoff.
        metadata_path: Raw GitHub REST release object fetched by bound release ID.
        binding_path: Initial empty-draft binding.
        output_path: Destination for the asset-complete binding.

    Returns:
        The asset-complete binding record.

    Raises:
        ReleaseValidationError: If release or asset identity differs from the binding.
    """
    binding = _read_release_binding(binding_path, require_assets=False)
    metadata = _read_json_object(metadata_path, "uploaded draft release")
    _validate_bound_release_identity(metadata, binding, draft=True, immutable=False)
    local_rows = _validated_local_release_rows(root)
    remote_rows = _validated_github_asset_rows(metadata, binding["expected_uploader"])
    if set(remote_rows) != set(local_rows):
        raise ReleaseValidationError("bound draft release asset names are not exact")
    assets: list[dict[str, Any]] = []
    for name in sorted(local_rows):
        asset_id, size, digest, uploader = remote_rows[name]
        if (size, digest) != local_rows[name]:
            raise ReleaseValidationError(f"bound draft release bytes differ for {name}")
        assets.append({
            "id": asset_id,
            "name": name,
            "size": size,
            "digest": digest,
            "uploader": uploader,
        })
    completed = {**binding, "assets": assets}
    _write_release_binding(output_path, completed)
    return completed


def validate_bound_draft_release(root: Path, metadata_path: Path, binding_path: Path) -> None:
    """Revalidate the same draft release and asset IDs immediately before PATCH.

    Args:
        root: Flat validated publisher handoff.
        metadata_path: Raw GitHub REST release object fetched by bound release ID.
        binding_path: Asset-complete release binding.

    Raises:
        ReleaseValidationError: If any bound release or asset identity changed.
    """
    binding = _read_release_binding(binding_path, require_assets=True)
    metadata = _read_json_object(metadata_path, "pre-publication draft release")
    _validate_bound_release_identity(metadata, binding, draft=True, immutable=False)
    local_rows = _validated_local_release_rows(root)
    bound_rows = _binding_asset_rows(binding)
    remote_rows = _validated_github_asset_rows(metadata, binding["expected_uploader"])
    if set(local_rows) != set(bound_rows) or remote_rows != bound_rows:
        raise ReleaseValidationError("pre-publication asset IDs or bytes changed after binding")
    if any(local_rows[name] != (bound_rows[name][1], bound_rows[name][2]) for name in local_rows):
        raise ReleaseValidationError("local publisher handoff changed after release binding")


def validate_published_release(
    binding_path: Path,
    id_metadata_path: Path,
    tag_metadata_path: Path,
) -> None:
    """Prove ID and tag immediately resolve to the same immutable published release.

    Args:
        binding_path: Asset-complete release binding used for the PATCH.
        id_metadata_path: Post-PATCH REST release object fetched by release ID.
        tag_metadata_path: Post-PATCH REST release object resolved from the tag.

    Raises:
        ReleaseValidationError: If publication identity, immutability, or assets differ.
    """
    binding = _read_release_binding(binding_path, require_assets=True)
    by_id = _read_json_object(id_metadata_path, "published release by ID")
    by_tag = _read_json_object(tag_metadata_path, "published release by tag")
    for metadata in (by_id, by_tag):
        _validate_bound_release_identity(metadata, binding, draft=False, immutable=True)
        if _validated_github_asset_rows(metadata, binding["expected_uploader"]) != _binding_asset_rows(binding):
            raise ReleaseValidationError("published release assets differ from the bound draft")


def _validated_local_release_rows(root: Path) -> dict[str, tuple[int, str]]:
    expected_names = RELEASE_ASSET_NAMES
    actual_names = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual_names != expected_names or any(path.is_dir() for path in root.iterdir()):
        raise ReleaseValidationError(f"publisher input is not flat and exact: {sorted(actual_names)}")
    manifest = _read_json_object(root / "manifest.json", "publisher release manifest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(EXPECTED_LEGS):
        raise ReleaseValidationError("publisher release manifest does not contain four artifact rows")
    manifest_rows: dict[str, tuple[int, str]] = {}
    for row in artifacts:
        if not isinstance(row, dict) or set(row) != {"platform", "accel", "file", "sha256", "size_bytes"}:
            raise ReleaseValidationError("publisher release manifest artifact fields are invalid")
        name = row["file"]
        if not isinstance(name, str) or name not in EXPECTED_LEGS or name in manifest_rows:
            raise ReleaseValidationError(f"publisher release manifest artifact name is invalid: {name!r}")
        if (row["platform"], row["accel"]) != EXPECTED_LEGS[name]:
            raise ReleaseValidationError(f"publisher release manifest selector is invalid: {name}")
        size = row["size_bytes"]
        digest = row["sha256"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not isinstance(digest, str)
            or SHA256_PATTERN.fullmatch(digest) is None
        ):
            raise ReleaseValidationError(f"publisher release manifest bytes are invalid: {name}")
        manifest_rows[name] = (size, digest)
    local_rows = {name: ((root / name).stat().st_size, sha256_file(root / name)) for name in expected_names}
    if any(local_rows[name] != manifest_rows[name] for name in EXPECTED_LEGS):
        raise ReleaseValidationError("publisher release manifest does not match local archive bytes")
    _validate_manifest_cuda_evidence(manifest, root)
    _validate_consumer_release_authority(root, manifest)
    return local_rows


def _validated_github_asset_rows(
    metadata: dict[str, Any],
    expected_uploader: str,
) -> dict[str, tuple[int, int, str, str]]:
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise ReleaseValidationError("GitHub release asset list is invalid")
    rows: dict[str, tuple[int, int, str, str]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseValidationError("GitHub release asset row is invalid")
        asset_id = asset.get("id")
        name = asset.get("name")
        size = asset.get("size")
        digest = asset.get("digest")
        uploader = asset.get("uploader")
        uploader_login = uploader.get("login") if isinstance(uploader, dict) else None
        if (
            not isinstance(asset_id, int)
            or isinstance(asset_id, bool)
            or asset_id <= 0
            or not isinstance(name, str)
            or name in rows
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or SHA256_PATTERN.fullmatch(digest.removeprefix("sha256:")) is None
            or asset.get("state") != "uploaded"
            or uploader_login != expected_uploader
        ):
            raise ReleaseValidationError(f"GitHub release asset identity is invalid: {name!r}")
        rows[name] = (asset_id, size, digest.removeprefix("sha256:"), uploader_login)
    if len({row[0] for row in rows.values()}) != len(rows):
        raise ReleaseValidationError("GitHub release asset IDs are not unique")
    return rows


def _validate_bound_release_identity(
    metadata: dict[str, Any],
    binding: dict[str, Any],
    *,
    draft: bool,
    immutable: bool,
) -> None:
    if (
        metadata.get("id") != binding["release_id"]
        or metadata.get("tag_name") != binding["tag_name"]
        or metadata.get("draft") is not draft
        or metadata.get("immutable") is not immutable
    ):
        raise ReleaseValidationError("GitHub release identity or state differs from the bound release ID")


def _binding_asset_rows(binding: dict[str, Any]) -> dict[str, tuple[int, int, str, str]]:
    return {row["name"]: (row["id"], row["size"], row["digest"], row["uploader"]) for row in binding["assets"]}


def _read_release_binding(path: Path, *, require_assets: bool) -> dict[str, Any]:
    binding = _read_json_object(path, "release binding")
    required = {
        "schema_version",
        "repository",
        "release_id",
        "tag_name",
        "source_commit",
        "expected_uploader",
        "assets",
    }
    if set(binding) != required or binding["schema_version"] != 1:
        raise ReleaseValidationError("release binding fields or schema version are invalid")
    if (
        not isinstance(binding["release_id"], int)
        or isinstance(binding["release_id"], bool)
        or binding["release_id"] <= 0
        or not isinstance(binding["repository"], str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", binding["repository"]) is None
        or not isinstance(binding["tag_name"], str)
        or re.fullmatch(r"[A-Za-z0-9._-]+", binding["tag_name"]) is None
        or not isinstance(binding["source_commit"], str)
        or COMMIT_PATTERN.fullmatch(binding["source_commit"]) is None
        or not isinstance(binding["expected_uploader"], str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[bot\])?", binding["expected_uploader"]) is None
        or not isinstance(binding["assets"], list)
    ):
        raise ReleaseValidationError("release binding identity is invalid")
    assets = binding["assets"]
    if require_assets and len(assets) != len(RELEASE_ASSET_NAMES):
        raise ReleaseValidationError("release binding does not contain the exact governed asset count")
    if not require_assets and assets:
        raise ReleaseValidationError("initial release binding must not contain assets")
    if assets:
        asset_fields = {"id", "name", "size", "digest", "uploader"}
        if any(not isinstance(row, dict) or set(row) != asset_fields for row in assets):
            raise ReleaseValidationError("release binding asset fields are invalid")
        rows = _binding_asset_rows(binding)
        if len(rows) != len(assets) or set(rows) != RELEASE_ASSET_NAMES:
            raise ReleaseValidationError("release binding asset set is invalid")
        if len({row[0] for row in rows.values()}) != len(rows):
            raise ReleaseValidationError("release binding asset IDs are not unique")
        for name, (asset_id, size, digest, uploader) in rows.items():
            if (
                not isinstance(asset_id, int)
                or isinstance(asset_id, bool)
                or asset_id <= 0
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
                or not isinstance(digest, str)
                or SHA256_PATTERN.fullmatch(digest) is None
                or uploader != binding["expected_uploader"]
            ):
                raise ReleaseValidationError(f"release binding asset identity is invalid: {name}")
    return binding


def _write_release_binding(path: Path, binding: dict[str, Any]) -> None:
    path.write_text(json.dumps(binding, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def extract_artifact_sboms(root: Path, output: Path) -> list[Path]:
    """Extract and validate each artifact-specific SPDX document for attestation.

    Args:
        root: Directory containing the four engine archives.
        output: Empty output directory for the attestation predicates.

    Returns:
        Extracted SPDX paths in artifact-name order.

    Raises:
        ReleaseValidationError: If an archive or SPDX document is invalid.
    """
    if output.exists() and any(output.iterdir()):
        raise ReleaseValidationError(f"SBOM output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for filename, (platform, accelerator) in EXPECTED_LEGS.items():
        archive_path = root / filename
        with zipfile.ZipFile(archive_path) as archive:
            try:
                content = archive.read(SPDX_MEMBER)
                index_content = archive.read(LICENSE_INDEX_MEMBER)
            except KeyError as exc:
                raise ReleaseValidationError(
                    f"{filename} is missing artifact-specific SPDX or license index evidence"
                ) from exc
        destination = output / f"{filename.removesuffix('.zip')}.spdx.json"
        destination.write_bytes(content)
        index_path = output / f".{filename}.index.json"
        index_path.write_bytes(index_content)
        try:
            _validate_spdx(
                destination,
                index_path=index_path,
                platform=platform,
                accelerator=accelerator,
            )
        finally:
            index_path.unlink(missing_ok=True)
        paths.append(destination)
    return paths


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReleaseValidationError(f"{label} must be a positive integer or null")
    return value


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseValidationError(f"{label} is unreadable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseValidationError(f"{label} is malformed JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ReleaseValidationError(f"{label} must be a JSON object")
    return payload


def _safe_extract_zip(archive_path: Path, destination: Path) -> None:
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseValidationError(f"engine archive is unreadable: {archive_path}") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_MEMBERS:
            raise ReleaseValidationError("engine archive member count is invalid")
        names: set[str] = set()
        expanded = 0
        for info in infos:
            name = info.filename
            pure = PurePosixPath(name)
            mode = info.external_attr >> 16
            if (
                not name
                or name in names
                or "\\" in name
                or pure.is_absolute()
                or ".." in pure.parts
                or info.is_dir()
                or stat.S_IFMT(mode) == stat.S_IFLNK
            ):
                raise ReleaseValidationError(f"engine archive has an unsafe or duplicate member: {name!r}")
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise ReleaseValidationError("engine archive exceeds the expanded-size safety limit")
            names.add(name)
            target = destination.joinpath(*pure.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_bytes(archive.read(info))
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ReleaseValidationError(f"engine archive member cannot be extracted: {name}") from exc
            if mode & stat.S_IXUSR:
                target.chmod(target.stat().st_mode | stat.S_IXUSR)


def _validate_spdx(path: Path, *, index_path: Path, platform: str, accelerator: str) -> None:
    document = _read_json_object(path, "artifact SPDX document")
    index = _read_json_object(index_path, "artifact license index")
    if document.get("spdxVersion") != "SPDX-2.3" or document.get("dataLicense") != "CC0-1.0":
        raise ReleaseValidationError("artifact SPDX document does not declare SPDX 2.3")
    namespace = document.get("documentNamespace")
    cargo_digest = index.get("cargo_identity_sha256")
    converter_digest = index.get("converter_identity_sha256")
    if (
        index.get("platform") != platform
        or index.get("accelerator") != accelerator
        or not isinstance(cargo_digest, str)
        or SHA256_PATTERN.fullmatch(cargo_digest) is None
        or not isinstance(converter_digest, str)
        or SHA256_PATTERN.fullmatch(converter_digest) is None
    ):
        raise ReleaseValidationError("artifact license index selector or dependency identity is invalid")
    expected_namespace_suffix = f"/{platform}/{accelerator}/{cargo_digest}/{converter_digest}"
    if not isinstance(namespace, str) or not namespace.endswith(expected_namespace_suffix):
        raise ReleaseValidationError("artifact SPDX namespace is not platform/accelerator specific")
    packages = document.get("packages")
    root_name = f"amw-engine-{platform}-{accelerator}"
    if not isinstance(packages, list):
        raise ReleaseValidationError("artifact SPDX document has no package list")
    roots = [package for package in packages if isinstance(package, dict) and package.get("name") == root_name]
    if len(roots) != 1 or not isinstance(roots[0].get("SPDXID"), str):
        raise ReleaseValidationError(f"artifact SPDX document does not describe {root_name}")
    root_id = roots[0]["SPDXID"]
    if document.get("documentDescribes") != [root_id]:
        raise ReleaseValidationError("artifact SPDX documentDescribes does not bind the artifact root")
    expected_dependencies: Counter[tuple[str, str, str, str]] = Counter()
    for key, ecosystem in (
        ("cargo_packages", "cargo"),
        ("converter_packages", "pypi"),
        ("native_components", "native"),
    ):
        rows = index.get(key)
        if not isinstance(rows, list):
            raise ReleaseValidationError(f"artifact license index has no valid {key} list")
        for row in rows:
            if not isinstance(row, dict):
                raise ReleaseValidationError(f"artifact license index contains an invalid {key} row")
            name = row.get("name")
            version = row.get("version")
            license_expression = row.get("license")
            if not all(isinstance(value, str) and value for value in (name, version, license_expression)):
                raise ReleaseValidationError(f"artifact license index contains an incomplete {key} identity")
            if not isinstance(name, str) or not isinstance(version, str) or not isinstance(license_expression, str):
                raise ReleaseValidationError(f"artifact license index contains an incomplete {key} identity")
            expected_dependencies[ecosystem, name, version, license_expression] += 1
    observed_dependencies: Counter[tuple[str, str, str, str]] = Counter()
    dependency_ids: set[str] = set()
    for package in packages:
        if not isinstance(package, dict) or package is roots[0]:
            continue
        name = package.get("name")
        version = package.get("versionInfo")
        license_expression = package.get("licenseDeclared")
        spdx_id = package.get("SPDXID")
        if not all(isinstance(value, str) and value for value in (name, version, license_expression, spdx_id)):
            raise ReleaseValidationError("artifact SPDX dependency identity is incomplete")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(license_expression, str)
            or not isinstance(spdx_id, str)
        ):
            raise ReleaseValidationError("artifact SPDX dependency identity is incomplete")
        external_refs = package.get("externalRefs", [])
        if external_refs:
            if not isinstance(external_refs, list) or len(external_refs) != 1 or not isinstance(external_refs[0], dict):
                raise ReleaseValidationError(f"artifact SPDX package reference is invalid for {name}")
            locator = external_refs[0].get("referenceLocator")
            cargo_locator = f"pkg:cargo/{name}@{version}"
            pypi_name = re.sub(r"[-_.]+", "-", name).lower()
            pypi_locator = f"pkg:pypi/{pypi_name}@{version}"
            if locator == cargo_locator:
                ecosystem = "cargo"
            elif locator == pypi_locator:
                ecosystem = "pypi"
            else:
                raise ReleaseValidationError(f"artifact SPDX package URL is invalid for {name}")
        else:
            ecosystem = "native"
        observed_dependencies[ecosystem, name, version, license_expression] += 1
        if spdx_id == root_id or spdx_id in dependency_ids:
            raise ReleaseValidationError("artifact SPDX dependency IDs are not unique")
        dependency_ids.add(spdx_id)
    if observed_dependencies != expected_dependencies:
        raise ReleaseValidationError("artifact SPDX dependency identities disagree with the license index")
    relationships = document.get("relationships")
    expected_relationships = {(root_id, "DEPENDS_ON", dependency_id) for dependency_id in dependency_ids}
    if not isinstance(relationships, list):
        raise ReleaseValidationError("artifact SPDX document has no dependency relationships")
    observed_relationships = {
        (row.get("spdxElementId"), row.get("relationshipType"), row.get("relatedSpdxElement"))
        for row in relationships
        if isinstance(row, dict)
    }
    if observed_relationships != expected_relationships or len(relationships) != len(expected_relationships):
        raise ReleaseValidationError("artifact SPDX dependency relationships do not exactly bind the indexed packages")


def _run_packaged_smokes(extracted: Path, *, platform: str, converter_python: Path) -> None:
    resolved_python = converter_python.resolve()
    if not resolved_python.is_absolute() or not resolved_python.is_file():
        raise ReleaseValidationError(f"converter interpreter is not an absolute executable path: {resolved_python}")
    if (platform == "windows") != (os.name == "nt"):
        raise ReleaseValidationError(f"cannot execute {platform} archive tools on this validation runner")
    suffix = ".exe" if platform == "windows" else ""
    commands = (
        ([str((extracted / f"amw-engine-server{suffix}").resolve()), "--version"], 30),
        ([str((extracted / export_tool_member("imatrix", platform=platform)).resolve()), "--help"], 30),
        ([str((extracted / f"llama-quantize{suffix}").resolve()), "--help"], 30),
        ([str(resolved_python), str((extracted / "convert_hf_to_gguf.py").resolve()), "--help"], 60),
        ([str(resolved_python), str((extracted / "convert_lora_to_gguf.py").resolve()), "--help"], 60),
    )
    for command, timeout in commands:
        try:
            completed = subprocess.run(
                command,
                cwd=extracted,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseValidationError(f"packaged executable smoke failed to run: {Path(command[0]).name}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout)[-1_000:]
            raise ReleaseValidationError(f"packaged executable smoke failed for {Path(command[0]).name}: {detail}")


def _identity_from_environment() -> ReleaseIdentity:
    keys = ("EXPECTED_REPOSITORY", "EXPECTED_COMMIT", "EXPECTED_REF", "EXPECTED_WORKFLOW", "EXPECTED_RUN_ID")
    missing = [key for key in keys if not os.environ.get(key)]
    if missing:
        raise ReleaseValidationError(f"release identity environment is incomplete: {', '.join(missing)}")
    return ReleaseIdentity(*(os.environ[key] for key in keys))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    policy = subparsers.add_parser("policy")
    policy.add_argument("--workflow", type=Path, required=True)
    policy.add_argument("--ci-requirements", type=Path, required=True)
    policy.add_argument("--ledger", type=Path, required=True)
    policy.add_argument("--cuda-evidence", type=Path, required=True)
    publication = subparsers.add_parser("publication-gate")
    publication.add_argument("--workflow", type=Path, required=True)
    publication.add_argument("--ledger", type=Path, required=True)
    publication.add_argument("--cuda-evidence", type=Path, required=True)
    publication.add_argument("--expected-commit", required=True)
    publication.add_argument("--expected-workflow-run-url", required=True)
    publication.add_argument("--expected-workflow-run-attempt", type=int, required=True)
    publication.add_argument("--expected-runner-id", type=int, required=True)
    publication.add_argument("--expected-runner-name", required=True)
    publication.add_argument("--expected-machine-id-sha256", required=True)
    publication.add_argument("--expected-service-account", required=True)
    publication.add_argument("--expected-work-directory", required=True)
    publication.add_argument("--certifier-public-key-der-base64", required=True)
    cuda_result = subparsers.add_parser("cuda-result")
    cuda_result.add_argument("--cuda-evidence", type=Path, required=True)
    cuda_result.add_argument("--expected-commit", required=True)
    cuda_result.add_argument("--expected-workflow-run-url", required=True)
    cuda_result.add_argument("--expected-workflow-run-attempt", type=int)
    cuda_result.add_argument("--expected-runner-id", type=int)
    cuda_result.add_argument("--expected-runner-name")
    cuda_result.add_argument("--expected-machine-id-sha256")
    cuda_result.add_argument("--expected-service-account")
    cuda_result.add_argument("--expected-work-directory")
    cuda_result.add_argument("--certifier-public-key-der-base64")
    bind_cuda_runner = subparsers.add_parser("bind-cuda-runner")
    bind_cuda_runner.add_argument("--cuda-evidence", type=Path, required=True)
    bind_cuda_runner.add_argument("--runner-metadata", type=Path, required=True)
    bind_cuda_runner.add_argument("--workflow-jobs", type=Path, required=True)
    bind_cuda_runner.add_argument("--subject-directory", type=Path, required=True)
    bind_cuda_runner.add_argument("--output", type=Path, required=True)
    bind_cuda_runner.add_argument("--expected-runner-id", type=int, required=True)
    bind_cuda_runner.add_argument("--expected-runner-name", required=True)
    bind_cuda_runner.add_argument("--expected-machine-id-sha256", required=True)
    bind_cuda_runner.add_argument("--expected-service-account", required=True)
    bind_cuda_runner.add_argument("--expected-work-directory", required=True)
    bind_cuda_runner.add_argument("--certifier-public-key-der-base64", required=True)
    bind_cuda_runner.add_argument("--expected-commit", required=True)
    bind_cuda_runner.add_argument("--expected-workflow-run-url", required=True)
    bind_cuda_runner.add_argument("--expected-workflow-run-attempt", type=int, required=True)
    size = subparsers.add_parser("size")
    size.add_argument("--bundle", required=True)
    size.add_argument("--binary", type=Path, required=True)
    size.add_argument("--ledger", type=Path, required=True)
    size.add_argument("--enforce", action="store_true")
    archive = subparsers.add_parser("archive")
    archive.add_argument("--archive", type=Path, required=True)
    archive.add_argument("--platform", choices=("windows", "linux"), required=True)
    archive.add_argument("--accelerator", choices=("cpu", "cuda"), required=True)
    archive.add_argument("--converter-python", type=Path)
    archive.add_argument("--run-smokes", action="store_true")
    merge = subparsers.add_parser("merge")
    merge.add_argument("--root", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    bootstrap_receipts = subparsers.add_parser("bootstrap-receipts")
    bootstrap_receipts.add_argument("--release-root", type=Path, required=True)
    bootstrap_receipts.add_argument("--receipts-root", type=Path, required=True)
    bootstrap_receipts.add_argument("--closures-root", type=Path, required=True)
    bootstrap_receipts.add_argument("--expected-commit", required=True)
    release = subparsers.add_parser("release-directory")
    release.add_argument("--root", type=Path, required=True)
    draft = subparsers.add_parser("draft-assets")
    draft.add_argument("--root", type=Path, required=True)
    draft.add_argument("--metadata", type=Path, required=True)
    bind_draft = subparsers.add_parser("bind-draft")
    bind_draft.add_argument("--metadata", type=Path, required=True)
    bind_draft.add_argument("--output", type=Path, required=True)
    bind_draft.add_argument("--repository", required=True)
    bind_draft.add_argument("--expected-tag", required=True)
    bind_draft.add_argument("--expected-commit", required=True)
    bind_draft.add_argument("--expected-uploader", required=True)
    bind_draft.add_argument("--github-env", type=Path)
    capture_draft = subparsers.add_parser("capture-draft-assets")
    capture_draft.add_argument("--root", type=Path, required=True)
    capture_draft.add_argument("--metadata", type=Path, required=True)
    capture_draft.add_argument("--binding", type=Path, required=True)
    capture_draft.add_argument("--output", type=Path, required=True)
    verify_draft = subparsers.add_parser("verify-bound-draft")
    verify_draft.add_argument("--root", type=Path, required=True)
    verify_draft.add_argument("--metadata", type=Path, required=True)
    verify_draft.add_argument("--binding", type=Path, required=True)
    verify_published = subparsers.add_parser("verify-published")
    verify_published.add_argument("--binding", type=Path, required=True)
    verify_published.add_argument("--id-metadata", type=Path, required=True)
    verify_published.add_argument("--tag-metadata", type=Path, required=True)
    sboms = subparsers.add_parser("extract-sboms")
    sboms.add_argument("--root", type=Path, required=True)
    sboms.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the AM Engine release validation CLI.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Zero on success and one with an actionable diagnostic on validation failure.
    """
    effective_argv = sys.argv[1:] if argv is None else argv
    if not effective_argv:
        try:
            validate_workflow_export_toolchain_bindings(Path(".github/workflows/engine.yml"))
        except (OSError, ReleaseValidationError) as exc:
            print(f"AM Engine release validation failed: {exc}", file=sys.stderr)
            return 1
        print("AM Engine release validation passed: export-toolchain-policy")
        return 0
    args = _build_parser().parse_args(effective_argv)
    try:
        if args.command == "policy":
            validate_workflow_size_bindings(args.workflow)
            validate_workflow_export_toolchain_bindings(args.workflow)
            validate_workflow_publication_bindings(args.workflow)
            validate_workflow_event_contract(args.workflow)
            validate_workflow_supply_chain_bindings(args.workflow, args.ci_requirements)
            validate_workflow_native_fixture_bindings(args.workflow)
            load_size_ledger(args.ledger)
            _validate_cuda_evidence(args.cuda_evidence)
        elif args.command == "publication-gate":
            validate_publication_prerequisites(
                workflow_path=args.workflow,
                ledger_path=args.ledger,
                cuda_evidence_path=args.cuda_evidence,
                expected_commit=args.expected_commit,
                expected_workflow_run_url=args.expected_workflow_run_url,
                expected_workflow_run_attempt=args.expected_workflow_run_attempt,
                expected_runner_id=args.expected_runner_id,
                expected_runner_name=args.expected_runner_name,
                expected_machine_id_sha256=args.expected_machine_id_sha256,
                expected_service_account=args.expected_service_account,
                expected_work_directory=args.expected_work_directory,
                certifier_public_key_der_base64=args.certifier_public_key_der_base64,
            )
        elif args.command == "cuda-result":
            validate_cuda_certification_result(
                args.cuda_evidence,
                expected_commit=args.expected_commit,
                expected_workflow_run_url=args.expected_workflow_run_url,
                expected_workflow_run_attempt=args.expected_workflow_run_attempt,
                expected_runner_id=args.expected_runner_id,
                expected_runner_name=args.expected_runner_name,
                expected_machine_id_sha256=args.expected_machine_id_sha256,
                expected_service_account=args.expected_service_account,
                expected_work_directory=args.expected_work_directory,
                certifier_public_key_der_base64=args.certifier_public_key_der_base64,
            )
        elif args.command == "bind-cuda-runner":
            bind_cuda_runner_inventory(
                args.cuda_evidence,
                args.runner_metadata,
                args.workflow_jobs,
                args.subject_directory,
                args.output,
                expected_runner_id=args.expected_runner_id,
                expected_runner_name=args.expected_runner_name,
                expected_machine_id_sha256=args.expected_machine_id_sha256,
                expected_service_account=args.expected_service_account,
                expected_work_directory=args.expected_work_directory,
                certifier_public_key_der_base64=args.certifier_public_key_der_base64,
                expected_commit=args.expected_commit,
                expected_workflow_run_url=args.expected_workflow_run_url,
                expected_workflow_run_attempt=args.expected_workflow_run_attempt,
            )
        elif args.command == "size":
            print(
                json.dumps(
                    validate_binary_size(
                        bundle=args.bundle,
                        binary=args.binary,
                        ledger_path=args.ledger,
                        enforce=args.enforce,
                    ),
                    sort_keys=True,
                )
            )
        elif args.command == "archive":
            validate_archive(
                args.archive,
                platform=args.platform,
                accelerator=args.accelerator,
                converter_python=args.converter_python,
                run_smokes=args.run_smokes,
            )
        elif args.command == "merge":
            merge_release_fragments(args.root, args.output, _identity_from_environment())
        elif args.command == "bootstrap-receipts":
            validate_bootstrap_receipts(
                release_root=args.release_root,
                receipts_root=args.receipts_root,
                closures_root=args.closures_root,
                expected_commit=args.expected_commit,
            )
        elif args.command == "release-directory":
            validate_release_directory(args.root, _identity_from_environment())
        elif args.command == "draft-assets":
            validate_draft_release_assets(args.root, args.metadata)
        elif args.command == "bind-draft":
            bind_draft_release(
                args.metadata,
                args.output,
                repository=args.repository,
                expected_tag=args.expected_tag,
                expected_commit=args.expected_commit,
                expected_uploader=args.expected_uploader,
                github_env_path=args.github_env,
            )
        elif args.command == "capture-draft-assets":
            capture_draft_release_assets(args.root, args.metadata, args.binding, args.output)
        elif args.command == "verify-bound-draft":
            validate_bound_draft_release(args.root, args.metadata, args.binding)
        elif args.command == "verify-published":
            validate_published_release(args.binding, args.id_metadata, args.tag_metadata)
        elif args.command == "extract-sboms":
            extract_artifact_sboms(args.root, args.output)
    except (OSError, ReleaseValidationError, zipfile.BadZipFile) as exc:
        print(f"AM Engine release validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"AM Engine release validation passed: {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
