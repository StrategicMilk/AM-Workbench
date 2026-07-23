"""Support bundle builder with redaction-first glob handling."""

from __future__ import annotations

import fnmatch
import logging
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.desktop.contracts import SupportBundleSpec

logger = logging.getLogger(__name__)

_RECURSIVE_GLOB_PATTERN = "**"


@dataclass(frozen=True, slots=True)
class BundleResult:
    """Runtime contract for BundleResult."""

    bundle_path: Path
    included_count: int
    redacted_count: int
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_path": str(self.bundle_path),
            "included_count": self.included_count,
            "redacted_count": self.redacted_count,
            "truncated": self.truncated,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BundleResult(bundle_path={self.bundle_path!r}, included_count={self.included_count!r}, redacted_count={self.redacted_count!r})"


class SupportBundleBuilder:
    """Runtime contract for SupportBundleBuilder."""

    def __init__(self, spec: SupportBundleSpec, *, source_root: Path | str = ".") -> None:
        self.spec = spec
        self.source_root = Path(source_root)

    def _is_redacted(self, rel: str) -> bool:
        name = Path(rel).name
        return any(
            fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern) for pattern in self.spec.redacted_globs
        )

    @staticmethod
    def _read_redacted_text(path: Path) -> tuple[str, bool] | None:
        """Return redacted text for UTF-8 files, or None for binary files."""
        from vetinari.safety.guardrails import redact_pii

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("Support bundle treating non-UTF-8 file %s as binary", path)
            return None
        redacted = redact_pii(content)
        return redacted, redacted != content

    def _validate_included_pattern(self, pattern: str) -> None:
        """Reject unbounded recursive patterns unless the caller opts in."""
        if _RECURSIVE_GLOB_PATTERN in Path(pattern).parts and not self.spec.allow_recursive_globs:
            raise ValueError("recursive support-bundle globs require allow_recursive_globs=True")

    def build(self) -> BundleResult:
        """Execute the build operation.

        Returns:
            BundleResult value produced by build().
        """
        destination = Path(self.spec.destination_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        included = 0
        redacted = 0
        total_bytes = 0
        truncated = False
        seen: set[Path] = set()
        matched_files = 0
        stat_calls = 0
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for pattern in self.spec.included_globs:
                self._validate_included_pattern(pattern)
                for path in self.source_root.glob(pattern):
                    if path in seen:
                        continue
                    if stat_calls >= self.spec.max_stat_calls or matched_files >= self.spec.max_matched_files:
                        truncated = True
                        break
                    try:
                        path_stat = path.stat()
                    except OSError:
                        logger.warning("Support bundle could not stat %s - skipping file", path, exc_info=True)
                        continue
                    stat_calls += 1
                    if not stat.S_ISREG(path_stat.st_mode):
                        continue
                    seen.add(path)
                    matched_files += 1
                    rel = path.relative_to(self.source_root).as_posix()
                    if self._is_redacted(rel):
                        redacted += 1
                        continue
                    try:
                        redacted_text = self._read_redacted_text(path)
                        size = len(redacted_text[0].encode("utf-8")) if redacted_text is not None else path_stat.st_size
                    except OSError:
                        logger.warning("Support bundle could not read %s - skipping file", path, exc_info=True)
                        continue
                    if total_bytes + size > self.spec.max_bytes:
                        truncated = True
                        continue
                    if redacted_text is None:
                        archive.write(path, rel)
                    else:
                        content, changed = redacted_text
                        archive.writestr(rel, content)
                        if changed:
                            redacted += 1
                    total_bytes += size
                    included += 1
                if truncated and (
                    stat_calls >= self.spec.max_stat_calls or matched_files >= self.spec.max_matched_files
                ):
                    break
        return BundleResult(destination, included, redacted, truncated)


__all__ = ["BundleResult", "SupportBundleBuilder"]
