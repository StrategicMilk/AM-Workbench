"""Runtime doctor — supported-matrix-driven runtime precondition checks.

Loads ``config/runtime/supported_matrix.yaml`` at call time (no module-level
I/O) and checks each row against the detected runtime. Fails closed on any
minimum-version violation or known-bad-range hit. Exists to satisfy Rule 8
(governance-rules.md): no unavailable-dependency pass-through; a missing or
unmet precondition surfaces as a blocker, never as a silent success.

Part of the runtime safety layer: Startup -> **Runtime Doctor** -> Subsystem Wiring.
The ``cmd_doctor`` CLI path invokes :func:`run_doctor` and returns exit code 2
on any blocker so automation scripts can gate on a clean precondition report.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from vetinari.agents.contracts import OutcomeSignal
from vetinari.runtime.backend_probes import default_probes
from vetinari.runtime.runtime_doctor_models import RuntimeCheckResult, RuntimeDoctorReport

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


# Default location of the supported matrix, relative to the repository root.
DEFAULT_MATRIX_PATH = _REPO_ROOT / "config/runtime/supported_matrix.yaml"

# Default staleness window when the matrix omits one.
DEFAULT_STALENESS_WINDOW_DAYS = 90


def load_matrix(path: Path) -> dict[str, Any]:
    """Load and minimally validate a supported-matrix YAML.

    Args:
        path: Filesystem path to the supported-matrix YAML.

    Returns:
        Parsed matrix as a dict with keys ``schema_version``,
        ``staleness_window_days``, ``components``.

    Raises:
        FileNotFoundError: If the matrix file does not exist.
        ValueError: If the file is missing required top-level keys.
    """
    if not path.exists():
        raise FileNotFoundError(f"Supported matrix not found at {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if "components" not in data or not isinstance(data["components"], list):
        raise ValueError(f"Supported matrix at {path} is missing a 'components' list")
    return data


def _parse_version(raw: str) -> tuple[int, ...]:
    """Parse a dotted numeric version into a tuple for comparison.

    Non-numeric suffixes (e.g., ``"2.7.0+cu128"``) are truncated at the first
    non-digit, non-dot character.

    Args:
        raw: Version string to parse.

    Returns:
        Tuple of ints representing the leading numeric components. Returns
        an empty tuple when the string yields no digits.
    """
    match = re.match(r"^(\d+(?:\.\d+)*)", raw.strip())
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _version_ge(candidate: str, minimum: str) -> bool:
    """Return True when ``candidate`` is >= ``minimum``.

    Args:
        candidate: Detected version.
        minimum: Required minimum.

    Returns:
        True when candidate satisfies the minimum, False otherwise.
    """
    return _parse_version(candidate) >= _parse_version(minimum)


def _version_matches_range(candidate: str, spec: str) -> bool:
    """Return True when ``candidate`` matches a known-bad-range spec.

    Supported spec forms:
        ``"==X.Y.Z"``   exact match
        ``">=X,<Y"``    compound comparison
        ``"X.Y.Z"``     treated as exact match

    Args:
        candidate: Detected version.
        spec: Range specification.

    Returns:
        True when the candidate falls inside the range.
    """
    spec = spec.strip()
    if spec.startswith("=="):
        return _parse_version(candidate) == _parse_version(spec[2:])
    if "," in spec:
        return all(_version_matches_range(candidate, part) for part in spec.split(","))
    if spec.startswith(">="):
        return _parse_version(candidate) >= _parse_version(spec[2:])
    if spec.startswith("<="):
        return _parse_version(candidate) <= _parse_version(spec[2:])
    if spec.startswith("<"):
        return _parse_version(candidate) < _parse_version(spec[1:])
    if spec.startswith(">"):
        return _parse_version(candidate) > _parse_version(spec[1:])
    return _parse_version(candidate) == _parse_version(spec)


def check_matrix_row(
    row: dict[str, Any],
    *,
    detected_version: str | None,
    detected_compute_capability: str | None,
) -> RuntimeCheckResult:
    """Evaluate one supported-runtime matrix row.

    Returns:
        Value produced for the caller.
    """
    component, minimum, known_bad_ranges, required_cc, sources, optional = _matrix_row_fields(row)
    platform_skip = row.get("platform_skip") or []
    import platform as _platform_mod

    if _platform_mod.system() in platform_skip:
        return _platform_skip_result(component, detected_version, sources, _platform_mod.system())
    if detected_version is None:
        return _missing_version_result(component, sources, optional)
    if minimum and not _version_ge(detected_version, str(minimum)):
        return RuntimeCheckResult(
            component=component,
            passed=False,
            detected_version=detected_version,
            reason=(
                f"{component} {detected_version} is older than the required minimum "
                f"{minimum}. See {', '.join(sources) if sources else 'matrix sources'}."
            ),
            matrix_sources=sources,
            is_blocker=True,
        )
    for bad_spec in known_bad_ranges:
        if _version_matches_range(detected_version, str(bad_spec)):
            return RuntimeCheckResult(
                component=component,
                passed=False,
                detected_version=detected_version,
                reason=(
                    f"{component} {detected_version} falls inside known-bad range "
                    f"{bad_spec!r}. Upgrade or pin away from this version. "
                    f"Sources: {', '.join(sources) if sources else 'matrix sources'}."
                ),
                matrix_sources=sources,
                is_blocker=True,
            )
    if (
        required_cc
        and detected_compute_capability is not None
        and not _version_ge(detected_compute_capability, str(required_cc))
    ):
        return RuntimeCheckResult(
            component=component,
            passed=True,
            detected_version=detected_version,
            reason=(
                f"{component} {detected_version} is installed, but detected GPU "
                f"compute capability {detected_compute_capability} is below the "
                f"row's required_compute_capability={required_cc}. Advisory only — "
                "the component will load but may not run on this GPU."
            ),
            matrix_sources=sources,
            is_blocker=False,
        )
    return RuntimeCheckResult(
        component=component,
        passed=True,
        detected_version=detected_version,
        reason=f"{component} {detected_version} satisfies the matrix.",
        matrix_sources=sources,
        is_blocker=True,
    )


def _matrix_row_fields(row: dict[str, Any]) -> tuple[str, Any, Any, Any, tuple[Any, ...], bool]:
    return (
        str(row.get("component", "<unknown>")),
        row.get("minimum_version"),
        row.get("known_bad_ranges") or [],
        row.get("required_compute_capability"),
        tuple(row.get("verified_sources") or ()),
        bool(row.get("optional")),
    )


def _missing_version_result(component: str, sources: tuple[Any, ...], optional: bool) -> RuntimeCheckResult:
    if optional:
        return RuntimeCheckResult(
            component=component,
            passed=True,
            detected_version=None,
            reason=(f"{component} is optional for this build and is not installed - skipping version check."),
            matrix_sources=sources,
            is_blocker=False,
        )
    return RuntimeCheckResult(
        component=component,
        passed=False,
        detected_version=None,
        reason=(f"Could not detect installed version of {component}; the supported matrix lists it as required."),
        matrix_sources=sources,
        is_blocker=True,
    )


def _platform_skip_result(
    component: str,
    detected_version: str | None,
    sources: tuple[Any, ...],
    platform_name: str,
) -> RuntimeCheckResult:
    return RuntimeCheckResult(
        component=component,
        passed=True,
        detected_version=detected_version,
        reason=f"{component} is not installed on this platform ({platform_name}) per supported-matrix policy - skipping version check.",
        matrix_sources=sources,
        is_blocker=False,
    )


def _detect_installed_version(component: str) -> str | None:
    """Return the installed version string for a known component, or None.

    Uses ``importlib.metadata`` — no heavy import chains — to keep the doctor
    fast and free of side effects.

    Args:
        component: Matrix row component name (e.g., "torch", "vllm").

    Returns:
        Version string, or None when the component is not installed.
    """
    from importlib import metadata

    package_map = {
        "python": None,  # handled separately
        "torch": "torch",
        "vllm": "vllm",
        "bitsandbytes": "bitsandbytes",
    }

    package = package_map.get(component)
    if component == "python":
        return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if not package:
        return None
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        logger.info(
            "Component %s is not installed in this environment — treating as missing in the doctor report",
            component,
        )
        return None


def _detect_compute_capability() -> str | None:
    """Return the highest GPU compute capability detected, or None.

    Uses pynvml when available; falls back to None without raising. The doctor
    must never crash on GPU detection — a missing GPU is a data point, not an
    error.

    Returns:
        Compute capability as "<major>.<minor>", or None.
    """
    try:
        pynvml = importlib.import_module("pynvml")

        pynvml.nvmlInit()
        try:
            count = pynvml.nvmlDeviceGetCount()
            best: tuple[int, int] | None = None
            for idx in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
                major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
                cc = (major, minor)
                if best is None or cc > best:
                    best = cc
            if best is None:
                return None
            return f"{best[0]}.{best[1]}"
        finally:
            pynvml.nvmlShutdown()
    except Exception as exc:
        logger.warning(
            "pynvml unavailable or GPU query failed (%s) — compute capability will be treated as unknown "
            "and any row with required_compute_capability skipped from advisory check",
            exc,
        )
        return None


def _staleness_warning(
    matrix_verified_at: datetime | None,
    staleness_window_days: int,
    now: datetime,
) -> str | None:
    """Return a staleness warning string when the matrix is older than the window.

    Args:
        matrix_verified_at: Parsed ISO date of the matrix's last verification.
        staleness_window_days: Configured staleness window (days).
        now: Current time.

    Returns:
        Warning string, or None when the matrix is fresh.
    """
    if matrix_verified_at is None:
        return (
            "Supported matrix is missing a verified_at date on every component; "
            "re-run matrix verification before relying on this report."
        )
    age = now - matrix_verified_at
    if age > timedelta(days=staleness_window_days):
        return (
            f"Supported matrix last verified {age.days} days ago "
            f"(window={staleness_window_days}d); re-run matrix verification before "
            "relying on this report."
        )
    return None


def run_doctor(
    matrix_path: Path | None = None,
    *,
    now: datetime | None = None,
    version_detector: Any | None = None,
    compute_capability_detector: Any | None = None,
) -> RuntimeDoctorReport:
    """Run the supported-matrix doctor and return a fail-closed report.

    Args:
        matrix_path: Path to the supported-matrix YAML. Defaults to
            ``config/runtime/supported_matrix.yaml`` relative to the current
            working directory.
        now: Current time override (for tests).
        version_detector: Callable ``(component: str) -> str | None`` overriding
            the default installed-version detection (for tests).
        compute_capability_detector: Callable ``() -> str | None`` overriding
            the default GPU compute-capability detection (for tests).

    Returns:
        RuntimeDoctorReport. ``passed=False`` when any blocker check failed or the
        matrix is stale with unknown component versions.
    """
    path = matrix_path or DEFAULT_MATRIX_PATH
    matrix = load_matrix(path)
    staleness_window = int(matrix.get("staleness_window_days", DEFAULT_STALENESS_WINDOW_DAYS))
    rows = matrix["components"]

    detect_version = version_detector or _detect_installed_version
    detect_cc = compute_capability_detector or _detect_compute_capability
    current_time = now or datetime.now(timezone.utc)

    detected_cc = detect_cc()
    checks: list[RuntimeCheckResult] = []
    verified_dates: list[datetime] = []
    for row in rows:
        verified_raw = row.get("verified_at")
        if verified_raw:
            try:
                parsed = datetime.fromisoformat(str(verified_raw)).replace(tzinfo=timezone.utc)
                verified_dates.append(parsed)
            except ValueError:
                logger.warning(
                    "Matrix row for %s has unparseable verified_at=%r — treating as stale",
                    row.get("component"),
                    verified_raw,
                )

        detected = detect_version(str(row.get("component", "")))
        checks.append(
            check_matrix_row(
                row,
                detected_version=detected,
                detected_compute_capability=detected_cc,
            )
        )

    matrix_verified_at = min(verified_dates) if verified_dates else None
    staleness_warning = _staleness_warning(matrix_verified_at, staleness_window, current_time)

    blockers = tuple(f"[{c.component}] {c.reason}" for c in checks if not c.passed and c.is_blocker)
    advisories = tuple(f"[{c.component}] {c.reason}" for c in checks if not c.passed and not c.is_blocker)

    # Rule 2: stale matrix + unknown component version -> blocker.
    if staleness_warning is not None and any(c.detected_version is None and c.is_blocker for c in checks):
        blockers = (
            *blockers,
            "Supported matrix is stale AND one or more required components "
            "have unknown installed versions — cannot certify preconditions.",
        )

    passed = not blockers
    return RuntimeDoctorReport(
        passed=passed,
        checks=tuple(checks),
        blockers=blockers,
        matrix_verified_at=matrix_verified_at,
        matrix_staleness_warning=staleness_warning,
        _advisory_failures=advisories,
    )


def format_report(report: RuntimeDoctorReport) -> str:
    """Format a RuntimeDoctorReport as a plain-text report suitable for CLI output.

    Args:
        report: The RuntimeDoctorReport to format.

    Returns:
        Multi-line string with one line per check, plus a final summary.
    """
    lines: list[str] = []
    for check in report.checks:
        marker = "PASS" if check.passed else ("BLOCK" if check.is_blocker else "WARN")
        detected = check.detected_version or "not-installed"
        lines.append(f"[{marker}] {check.component} ({detected}): {check.reason}")
    if report.matrix_staleness_warning:
        lines.append(f"[WARN] matrix staleness: {report.matrix_staleness_warning}")
    summary = "Runtime doctor: PASS" if report.passed else "Runtime doctor: FAIL (blockers found)"
    lines.append(summary)
    return "\n".join(lines)


def run_backend_probes() -> tuple[OutcomeSignal, ...]:
    """Run registered backend probes without changing matrix doctor semantics.

    Backend packages are optional and often live outside the base Python
    environment, so these probes are exposed as an additive health surface
    instead of converting missing optional backends into runtime startup
    blockers.
    """
    return tuple(probe.probe_fn() for probe in default_probes().values())


def format_backend_probe_report(signals: tuple[OutcomeSignal, ...]) -> str:
    """Format backend probe outcomes for operator-facing doctor output.

    Returns:
        Multiline backend probe summary.
    """
    lines: list[str] = []
    for signal in signals:
        marker = "PASS" if signal.passed else "WARN"
        reason = "; ".join(signal.issues) or "backend healthy"
        lines.append(f"[{marker}] backend probe: {reason}")
    lines.append(f"Backend probes: {sum(1 for signal in signals if signal.passed)}/{len(signals)} passed")
    return "\n".join(lines)


def validate_runtime_version(
    component: str,
    version: str,
    *,
    matrix_path: Path | None = None,
    compute_capability: str | None = None,
) -> RuntimeCheckResult:
    """Check a single component version against the supported matrix.

    For runtime-discovered versions (e.g. vLLM engine_version pulled from a
    container's ``/version`` endpoint) where ``importlib.metadata`` does not
    apply. Adapters use this to gate their own init when a known-bad release
    is detected on supported hardware.

    Args:
        component: Matrix row component name (e.g. ``"vllm"``).
        version: Version string discovered at runtime (e.g. ``"0.18.1"``).
        matrix_path: Override matrix YAML path (test seam).
        compute_capability: Override detected GPU compute capability (test
            seam). Defaults to the local pynvml-detected value.

    Returns:
        RuntimeCheckResult for the matrix row matching ``component``.

    Raises:
        ValueError: If ``component`` does not appear in the matrix.
        FileNotFoundError: Propagated from :func:`load_matrix`.
    """
    path = matrix_path or DEFAULT_MATRIX_PATH
    matrix = load_matrix(path)
    detected_cc = compute_capability if compute_capability is not None else _detect_compute_capability()
    for row in matrix["components"]:
        if str(row.get("component")) == component:
            return check_matrix_row(
                row,
                detected_version=version,
                detected_compute_capability=detected_cc,
            )
    raise ValueError(f"Component {component!r} is not declared in supported matrix at {path}")


def cli_doctor_report(matrix_path: Path | None = None) -> tuple[int, str]:
    """Return (exit_code, formatted_report) for a CLI doctor invocation.

    Returns the formatted text instead of printing so the caller (cmd_doctor in
    vetinari/cli_packaging_doctor.py or any other CLI frontend) decides whether
    to emit via logger, rich console, or plain stdout. This keeps print() out
    of the production module while still exposing a wired call site.

    Args:
        matrix_path: Optional path to a supported-matrix YAML.

    Returns:
        Tuple of (exit_code, formatted_report). Exit code 0 on pass, 1 on
        advisory-only failure, 2 on any blocker.
    """
    report = run_doctor(matrix_path)
    rendered = format_report(report)
    if not report.passed:
        return 2, rendered
    if report._advisory_failures:
        return 1, rendered
    return 0, rendered
