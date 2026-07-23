"""Migration source configuration loading."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

import yaml

from vetinari.constants import OUTPUTS_DIR, PROJECT_ROOT
from vetinari.workbench.migration.contracts import MigrationSourceKind, MigrationSourceSpec

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "workbench" / "migration_sources.yaml"
_DEFAULT_STATE_DIR = OUTPUTS_DIR / "workbench" / "spine" / "migration"
_LEDGER_FILENAME = "migration_runs.jsonl"
_CLAUDE_CONFIG_CATALOG_SOURCE_ID = "claude_config"
_CLAUDE_CONFIG_CATALOG_PATHS = (".claude/settings.json", ".claude/CLAUDE.md")
_PROVIDER_MODEL_CATALOG_SOURCE_ID = "provider_models"
_PROVIDER_MODEL_CATALOG_PATHS = ("config/models.yaml", "models.yaml")
_SECRET_NAME_RE = re.compile(r"(secret|token|credential|password|api[_-]?key|\.env)", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]+)"
)


class WorkbenchMigrationError(RuntimeError):
    """Raised when migration state cannot be trusted."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(*parts: object) -> str:
    raw = "\0".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _redact(text: str) -> str:
    text = _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    if len(text) > 500:
        text = text[:500] + "\n<preview-truncated>"
    return text


def _safe_rel_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return path.name


def _safe_destination_fragment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "item"


def default_migration_config_path() -> Path:
    return _DEFAULT_CONFIG_PATH


def default_migration_state_dir() -> Path:
    return _DEFAULT_STATE_DIR


def migration_ledger_filename() -> str:
    return _LEDGER_FILENAME


def migration_utc_now_iso() -> str:
    return _utc_now_iso()


def stable_migration_id(*parts: object) -> str:
    return _stable_id(*parts)


def redact_migration_preview(text: str) -> str:
    return _redact(text)


def safe_migration_rel_path(path: Path, root: Path) -> str:
    return _safe_rel_path(path, root)


def safe_migration_destination_fragment(value: str) -> str:
    return _safe_destination_fragment(value)


def is_secret_like_migration_source(path: Path, preview_text: str) -> bool:
    return _SECRET_NAME_RE.search(path.name) is not None or _SECRET_VALUE_RE.search(preview_text) is not None


def _default_source_specs() -> tuple[MigrationSourceSpec, ...]:
    return (
        MigrationSourceSpec(
            "workbench_exports", "Workbench exports", MigrationSourceKind.WORKBENCH_EXPORT, ("exports/workbench",)
        ),
        MigrationSourceSpec(
            "codex_config",
            "Codex configuration",
            MigrationSourceKind.CODEX_CONFIG,
            (".codex/config.toml", ".codex/AGENTS.md"),
        ),
        MigrationSourceSpec(
            _CLAUDE_CONFIG_CATALOG_SOURCE_ID,
            "Claude configuration",
            MigrationSourceKind.CLAUDE_CONFIG,
            _CLAUDE_CONFIG_CATALOG_PATHS,
        ),
        MigrationSourceSpec(
            _PROVIDER_MODEL_CATALOG_SOURCE_ID,
            "Provider settings",
            MigrationSourceKind.PROVIDER_MODEL_SETTING,
            _PROVIDER_MODEL_CATALOG_PATHS,
        ),
        MigrationSourceSpec("skills", "Skills", MigrationSourceKind.SKILL, (".codex/skills",)),
        MigrationSourceSpec(
            "memories", "Memories", MigrationSourceKind.MEMORY, (".codex/memories", ".claude/memories")
        ),
        MigrationSourceSpec(
            "automations", "Automations", MigrationSourceKind.AUTOMATION, ("automations", ".codex/automations")
        ),
        MigrationSourceSpec(
            "tool_settings",
            "MCP and tool settings",
            MigrationSourceKind.TOOL_SETTING,
            (".codex/mcp.json", ".claude/mcp.json", "tools.json"),
            True,
        ),
        MigrationSourceSpec(
            "workspace_packs", "Workspace packs", MigrationSourceKind.WORKSPACE_PACK, ("packs", "workspace_packs")
        ),
        MigrationSourceSpec(
            "external_assistant_data",
            "External assistant data",
            MigrationSourceKind.EXTERNAL_ASSISTANT_DATA,
            ("external_assistants", "assistant_exports"),
        ),
    )


def load_migration_source_specs(config_path: Path | str = _DEFAULT_CONFIG_PATH) -> tuple[MigrationSourceSpec, ...]:
    """Load migration source specs, falling back to built-ins when config is absent.

    Returns:
        Resolved migration source specs value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    path = Path(config_path)
    if not path.exists():
        return _default_source_specs()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise WorkbenchMigrationError(f"migration source config is unreadable: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise WorkbenchMigrationError("migration source config must be a mapping")
    specs: list[MigrationSourceSpec] = []
    for row in raw.get("sources", ()):
        if not isinstance(row, Mapping):
            raise WorkbenchMigrationError("migration source rows must be mappings")
        try:
            paths = tuple(str(item) for item in row["paths"])
            specs.append(
                MigrationSourceSpec(
                    source_id=str(row["id"]),
                    label=str(row["label"]),
                    kind=MigrationSourceKind(str(row["kind"])),
                    paths=paths,
                    risky_tool=bool(row.get("risky_tool", False)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkbenchMigrationError(f"invalid migration source row: {row!r}") from exc
    return tuple(specs) or _default_source_specs()
