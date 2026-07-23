"""Redaction-first update support bundle builder."""

from __future__ import annotations

import json
import logging
import re
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.desktop.contracts import SupportBundleSpec
from vetinari.desktop.support_bundle import SupportBundleBuilder
from vetinari.workbench.update_safety.contracts import SupportBundleBuildResult, UpdateReadiness, UpdateReadinessState

logger = logging.getLogger(__name__)


DEFAULT_SUPPORT_BUNDLE_ROOT = OUTPUTS_DIR / "workbench" / "spine" / "update-safety" / "support-bundles"
_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential|authorization|private)")
_SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{8,}|sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|AKIA[0-9A-Z]{12,})"
)
_PRIVATE_NAMES = {".env", "id_rsa", "id_ed25519"}


class UpdateSupportBundleBuilder:
    """Build update support bundles without leaking secrets or private artifact bodies."""

    def __init__(
        self,
        *,
        source_root: str | Path = ".",
        output_root: str | Path = DEFAULT_SUPPORT_BUNDLE_ROOT,
        max_bytes: int = 10_000_000,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.output_root = Path(output_root).resolve()
        self.max_bytes = int(max_bytes)

    def build(
        self,
        *,
        destination_path: str | Path | None = None,
        readiness: UpdateReadiness | None = None,
        health_summary: dict[str, Any] | None = None,
        extension_status: dict[str, Any] | None = None,
        recent_run_ids: tuple[str, ...] = (),
        version_build: dict[str, Any] | None = None,
    ) -> SupportBundleBuildResult:
        """Execute the build operation.

        Returns:
            SupportBundleBuildResult value produced by build().
        """
        try:
            destination = self._resolve_destination(destination_path)
        except ValueError as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return SupportBundleBuildResult(
                state=UpdateReadinessState.BLOCKED,
                bundle_path="",
                reasons=(str(exc),),
            )
        redacted_payloads = _redacted_staging_payloads(
            readiness=readiness,
            health_summary=health_summary,
            extension_status=extension_status,
            recent_run_ids=recent_run_ids,
            version_build=version_build,
        )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            desktop_spec = SupportBundleSpec(
                destination_path=destination.with_suffix(".desktop.zip"),
                included_globs=("logs/*.log", f"{OUTPUTS_DIR.name}/workbench/launcher/*.json"),
                redacted_globs=("**/secret*", "**/credentials*", "**/*.key", "**/.env*"),
                max_bytes=min(self.max_bytes, 2_000_000),
            )
            SupportBundleBuilder(desktop_spec, source_root=self.source_root).build()
            archive_result, included, redacted = self._write_redacted_archive(destination, redacted_payloads)
            if archive_result is not None:
                return archive_result
        except OSError as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return SupportBundleBuildResult(
                UpdateReadinessState.BLOCKED, "", reasons=(f"support_bundle_write_failed:{type(exc).__name__}",)
            )
        finally:
            desktop_zip = destination.with_suffix(".desktop.zip")
            with suppress(OSError):
                desktop_zip.unlink()
        return SupportBundleBuildResult(
            state=UpdateReadinessState.READY,
            bundle_path=str(destination),
            included_files=tuple(included),
            redacted_files=tuple(redacted),
            reasons=("support_bundle_created",),
            metadata={"max_bytes": self.max_bytes},
        )

    def _write_redacted_archive(
        self,
        destination: Path,
        redacted_payloads: dict[str, Any],
    ) -> tuple[SupportBundleBuildResult | None, tuple[str, ...], tuple[str, ...]]:
        included: list[str] = []
        redacted: list[str] = []
        total = 0
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            try:
                for name, payload in redacted_payloads.items():
                    total = _write_json_member(archive, name, payload, total, self.max_bytes)
                    included.append(name)
            except _SupportBundleSizeError:
                logger.warning("Exception handled by  write redacted archive fallback", exc_info=True)
                return _size_blocked(), tuple(included), tuple(redacted)
            for rel in _safe_support_files(self.source_root):
                source = (self.source_root / rel).resolve()
                if _is_private_path(source):
                    redacted.append(rel)
                    continue
                try:
                    data = source.read_bytes()
                except OSError:
                    logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                    return (
                        SupportBundleBuildResult(
                            UpdateReadinessState.BLOCKED,
                            "",
                            reasons=(f"support_input_unreadable:{rel}",),
                        ),
                        tuple(included),
                        tuple(redacted),
                    )
                total += len(data)
                if total > self.max_bytes:
                    return _size_blocked(), tuple(included), tuple(redacted)
                archive.writestr(rel, _redact_text(data.decode("utf-8", errors="replace")).encode("utf-8"))
                included.append(rel)
            try:
                _write_json_member(
                    archive,
                    "redaction_manifest.json",
                    {"redacted_files": redacted, "redaction_wins": True},
                    total,
                    self.max_bytes,
                )
            except _SupportBundleSizeError:
                logger.warning("Exception handled by  write redacted archive fallback", exc_info=True)
                return _size_blocked(), tuple(included), tuple(redacted)
            included.append("redaction_manifest.json")
        return None, tuple(included), tuple(redacted)

    def _resolve_destination(self, destination_path: str | Path | None) -> Path:
        self.output_root.mkdir(parents=True, exist_ok=True)
        destination = (
            Path(destination_path) if destination_path is not None else self.output_root / "update-support-bundle.zip"
        )
        if not destination.is_absolute():
            destination = self.output_root / destination
        resolved = destination.resolve()
        if not resolved.is_relative_to(self.output_root):
            raise ValueError("support_bundle_destination_outside_root")
        if resolved.exists():
            resolved = resolved.with_name(f"{resolved.stem}-next{resolved.suffix}")
        return resolved


class _SupportBundleSizeError(ValueError):
    """Raised internally when support bundle size bounds are exceeded."""


def _size_blocked() -> SupportBundleBuildResult:
    return SupportBundleBuildResult(
        UpdateReadinessState.BLOCKED,
        "",
        reasons=("support_bundle_size_limit_exceeded",),
    )


def _redacted_staging_payloads(
    *,
    readiness: UpdateReadiness | None,
    health_summary: dict[str, Any] | None,
    extension_status: dict[str, Any] | None,
    recent_run_ids: tuple[str, ...],
    version_build: dict[str, Any] | None,
) -> dict[str, Any]:
    staging_payloads = {
        "update_readiness.json": readiness.to_dict() if readiness else {"state": "unavailable"},
        "health_summary.json": health_summary or {"status": "not_provided"},
        "version_build.json": version_build or {"version": "unknown", "build": "unknown"},
        "extension_status.json": extension_status or {"extensions": []},
        "recent_runs.json": {"recent_run_ids": list(recent_run_ids)},
    }
    return {name: redact_update_support_payload(payload) for name, payload in staging_payloads.items()}


def _write_json_member(
    archive: zipfile.ZipFile,
    name: str,
    payload: Any,
    current_total: int,
    max_bytes: int,
) -> int:
    data = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
    total = current_total + len(data)
    if total > max_bytes:
        raise _SupportBundleSizeError("support_bundle_size_limit_exceeded")
    archive.writestr(name, data)
    return total


def redact_update_support_payload(value: Any) -> Any:
    """Redact secret keys and token-looking string values.

    Returns:
        Any value produced by redact_update_support_payload().
    """
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _SECRET_KEY_RE.search(str(key)) else redact_update_support_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_update_support_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_update_support_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _safe_support_files(root: Path) -> tuple[str, ...]:
    candidates = []
    for pattern in ("logs/*.log", "config/workbench/*.yaml", f"{OUTPUTS_DIR.name}/workbench/*.json"):
        candidates.extend(path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file())
    return tuple(sorted(set(candidates)))


def _is_private_path(path: Path) -> bool:
    name = path.name.lower()
    return name in _PRIVATE_NAMES or bool(_SECRET_KEY_RE.search(path.as_posix()))


def _redact_yaml_line(line: str) -> str:
    """Redact YAML lines whose key matches secret-key patterns.

    Handles both bare-value lines (``key: value``) and quoted-value lines.
    Value-shape redaction via ``_SECRET_VALUE_RE`` runs afterwards on all lines.

    Args:
        line: A single YAML text line.

    Returns:
        The line with the value portion replaced by ``[redacted]`` when the key
        name matches ``_SECRET_KEY_RE``, or the original line otherwise.
    """
    # Match ``  key: value`` or ``key: value`` (optional leading whitespace).
    m = re.match(r"^(\s*\S[^:]*:\s*)(.*)", line)
    if m:
        key_part, value_part = m.group(1), m.group(2)
        # Strip YAML inline comments from key name for pattern matching.
        key_name = key_part.rstrip(": \t")
        if value_part.strip() and _SECRET_KEY_RE.search(key_name):
            return key_part + "[redacted]"
    return line


def _redact_text(value: str) -> str:
    lines = value.splitlines(keepends=True)
    redacted_lines = [_redact_yaml_line(line) for line in lines]
    rejoined = "".join(redacted_lines)
    return _SECRET_VALUE_RE.sub("[redacted]", rejoined)


__all__ = ["DEFAULT_SUPPORT_BUNDLE_ROOT", "UpdateSupportBundleBuilder", "redact_update_support_payload"]
