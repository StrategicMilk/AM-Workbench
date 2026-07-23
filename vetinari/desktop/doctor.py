"""Launcher doctor checks for local desktop readiness."""

from __future__ import annotations

import json
import logging
import socket
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import OUTPUTS_DIR

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """Runtime contract for DoctorCheck."""

    name: str
    passed: bool
    blockers: tuple[str, ...] = ()
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "blockers": list(self.blockers),
            "remediation": self.remediation,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DoctorCheck(name={self.name!r}, passed={self.passed!r}, blockers={self.blockers!r})"


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Runtime contract for DoctorReport."""

    checks: tuple[DoctorCheck, ...]
    blockers: tuple[str, ...]
    generated_at: datetime

    @property
    def overall_passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [check.to_dict() for check in self.checks],
            "overall_passed": self.overall_passed,
            "blockers": list(self.blockers),
            "generated_at": self.generated_at.isoformat(),
        }


_DEFAULT_OUTPUT_DIR = OUTPUTS_DIR / "workbench" / "launcher"


class LauncherDoctor:
    """Runtime contract for LauncherDoctor."""

    def __init__(
        self,
        *,
        default_backend_port: int = 8000,
        output_dir: Path | str = _DEFAULT_OUTPUT_DIR,
        source_root: Path | str | None = None,
    ) -> None:
        self.default_backend_port = default_backend_port
        self.output_dir = Path(output_dir)
        self.source_root = Path(source_root) if source_root is not None else Path.cwd()

    def _port_check(self) -> DoctorCheck:
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", self.default_backend_port))
        except OSError:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return DoctorCheck(
                "backend_port",
                False,
                (f"port {self.default_backend_port} is already in use",),
                "Stop the conflicting process or change the launcher backend port.",
            )
        finally:
            sock.close()
        return DoctorCheck("backend_port", True)

    @staticmethod
    def _file_check(name: str, path: Path) -> DoctorCheck:
        if not path.exists():
            return DoctorCheck(name, False, (f"{path} missing",), "Regenerate or repair the upstream Workbench pack.")
        if path.suffix in {".json", ".yaml", ".yml"}:
            try:
                if path.suffix == ".json":
                    json.loads(path.read_text(encoding="utf-8"))
                else:
                    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict):
                        msg = f"{path} root must be a mapping"
                        return DoctorCheck(name, False, (msg,), "Repair the configuration file.")
            except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
                logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                return DoctorCheck(name, False, (f"{path} unreadable: {exc}",), "Repair the configuration file.")
        return DoctorCheck(name, True)

    def _source_path(self, relative: str) -> Path:
        return self.source_root / relative

    def run(self) -> DoctorReport:
        """Execute the run operation.

        Returns:
            DoctorReport value produced by run().
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        checks = (
            self._port_check(),
            self._file_check("model_registry", self._source_path("vetinari/workbench/model_registry.py")),
            self._file_check(
                "capability_pack_manifest",
                self._source_path("config/workbench/capability_packs/base.yaml"),
            ),
            self._file_check("resource_governor_config", self._source_path("config/workbench/resource_governor.yaml")),
            self._file_check(
                "concurrency_profiles_config",
                self._source_path("config/workbench/concurrency_profiles.yaml"),
            ),
            DoctorCheck(
                "launcher_output_writable",
                self.output_dir.exists(),
                () if self.output_dir.exists() else ("output dir unavailable",),
            ),
        )
        blockers = tuple(blocker for check in checks for blocker in check.blockers)
        report = DoctorReport(checks=checks, blockers=blockers, generated_at=datetime.now(timezone.utc))
        target = self.output_dir / "doctor_report.json"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=target.parent, delete=False) as tmp:
            tmp.write(json.dumps(report.to_dict(), indent=2) + "\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(target)
        return report


__all__ = ["DoctorCheck", "DoctorReport", "LauncherDoctor"]
