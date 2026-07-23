"""Watch Mode — file monitoring and automatic task execution.

Monitors a codebase for file changes and reacts to special comments
like ``# @vetinari fix this`` or ``# @vetinari add tests``.

Does NOT use the watchdog library (to avoid extra dependencies).
Uses polling-based file monitoring with configurable intervals.

Directive report entries are appended as newline-delimited JSON to
``.vetinari/watch_report.jsonl`` in the watched directory so that
other tools (CI, editors, dashboards) can consume them.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import THREAD_JOIN_TIMEOUT
from vetinari.privacy.envelope import PRIVACY_ENVELOPE_KEY, privacy_receipt, require_privacy_envelope
from vetinari.watch_models import (
    DIRECTIVE_PATTERNS,
    DirectiveReport,
    FileChange,
    VetinariDirective,
    WatchConfig,
)

logger = logging.getLogger(__name__)


def _privacy_safe_report_text(value: str | None) -> str | None:
    """Redact directive text before durable watch-report persistence."""
    if value is None:
        return None
    try:
        from vetinari.security.redaction import redact_text

        return redact_text(value)
    except Exception:
        logger.warning("Watch report redaction unavailable; storing conservative placeholder", exc_info=True)
        return "[REDACTION_UNAVAILABLE]"


class FileWatcher:
    """Polling-based file watcher."""

    def __init__(self, config: WatchConfig | None = None):
        self._config = config or WatchConfig()
        self._file_states: dict[str, float] = {}  # path -> mtime
        # Tracks whether the initial baseline scan has completed. Separate from
        # _file_states so an empty-tree initial scan still marks initialization.
        self._initialized: bool = False
        self.is_running = False
        self._callbacks: list[Callable[[list[FileChange]], None]] = []
        self._thread: threading.Thread | None = None
        # Guards start/stop so only one polling thread runs at a time.
        self._thread_lock: threading.Lock = threading.Lock()

    def on_change(self, callback: Callable[[list[FileChange]], None]) -> None:
        """Register a callback for file changes."""
        self._callbacks.append(callback)

    def scan(self) -> list[FileChange]:
        """Scan for file changes since last scan.

        Returns:
            List of results.
        """
        changes: list[FileChange] = []
        current_files: set[str] = set()

        watch_path = Path(self._config.watch_dir)
        for pattern in self._config.include_patterns:
            for file_path in watch_path.rglob(pattern):
                str_path = str(file_path)

                # Check exclusions
                if self._is_excluded(str_path):
                    continue

                current_files.add(str_path)
                try:
                    mtime = file_path.stat().st_mtime
                    size = file_path.stat().st_size
                except OSError:
                    logger.warning("Could not stat file %s during watch scan — skipping", str_path)
                    continue

                if str_path not in self._file_states:
                    # D2 fix: use _initialized flag instead of checking whether
                    # _file_states is non-empty. An empty-tree initial scan must
                    # still arm the baseline so later creates are detected.
                    if self._initialized:
                        changes.append(FileChange(str_path, "created", size=size))
                elif mtime > self._file_states[str_path]:
                    changes.append(FileChange(str_path, "modified", size=size))

                self._file_states[str_path] = mtime

        # Check for deleted files
        for old_path in set(self._file_states.keys()) - current_files:
            changes.append(FileChange(old_path, "deleted"))
            del self._file_states[old_path]

        # Mark baseline established after first scan completes.
        self._initialized = True

        return changes

    def _is_excluded(self, path: str) -> bool:
        """Check if a path matches any exclusion pattern.

        Directory patterns (no wildcard) are matched as path components to
        avoid false positives (e.g., '.git' should not exclude '.github').
        Glob patterns (containing '*') are matched as substrings.
        """
        parts = Path(path).parts
        for pattern in self._config.exclude_patterns:
            if "*" in pattern:
                # Glob-style: substring match (e.g., '*.pyc')
                if any(fnmatch.fnmatch(p, pattern) for p in parts):
                    return True
            else:
                # Directory/exact name: component match
                if pattern in parts:
                    return True
        return False

    def start(self) -> None:
        """Start watching in a background thread.

        D1 fix: acquires _thread_lock and joins any still-alive previous thread
        before creating a new one, preventing duplicate polling threads on
        stop/restart cycles.
        """
        with self._thread_lock:
            if self.is_running:
                return
            # Join a leftover thread that may not have exited yet.
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=THREAD_JOIN_TIMEOUT)
            self.is_running = True
            # Initial scan to establish baseline
            self.scan()
            self._thread = threading.Thread(target=self._watch_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop watching and join the background thread.

        D1 fix: acquires _thread_lock so that a concurrent restart cannot
        create a second polling thread before this one has fully exited.
        """
        with self._thread_lock:
            self.is_running = False
            if self._thread:
                self._thread.join(timeout=THREAD_JOIN_TIMEOUT)
                self._thread = None

    def _watch_loop(self) -> None:
        """Main watch loop."""
        while self.is_running:
            changes = self.scan()
            if changes:
                for callback in self._callbacks:
                    try:
                        callback(changes)
                    except Exception as e:
                        logger.error("Watch callback error: %s", e)
            time.sleep(self._config.poll_interval)


class DirectiveScanner:
    """Scans files for @vetinari directives."""

    def __init__(self, max_file_size: int = 1_000_000):
        self._max_file_size = max_file_size

    def scan_file(self, file_path: str) -> list[VetinariDirective]:
        """Scan a single file for directives.

        Returns:
            List of results.
        """
        directives = []
        try:
            path = Path(file_path)
            if path.stat().st_size > self._max_file_size:
                return []

            with Path(file_path).open(encoding="utf-8", errors="ignore") as f:
                for line_num, line in enumerate(f, 1):
                    for pattern in DIRECTIVE_PATTERNS:
                        match = pattern.search(line)
                        if match:
                            directives.append(
                                VetinariDirective(
                                    file_path=file_path,
                                    line_number=line_num,
                                    directive=match.group(1).strip(),
                                    full_line=line.strip(),
                                ),
                            )
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Could not scan %s: %s", file_path, e)
        return directives

    def scan_changes(self, changes: list[FileChange]) -> list[VetinariDirective]:
        """Scan changed files for directives.

        Returns:
            List of results.
        """
        directives = []
        for change in changes:
            if change.change_type != "deleted":
                directives.extend(self.scan_file(change.path))
        return directives


class WatchMode:
    """Main watch mode coordinator.

    Detects ``@vetinari`` directives in source files as they change and
    dispatches them to typed handlers.  Each processed directive is:

    1. Logged at INFO level for immediate observability.
    2. Added to an in-memory ``task_queue`` so callers can poll it.
    3. Appended as a JSON line to ``.vetinari/watch_report.jsonl``
       inside the watched directory for persistent, tool-friendly output.
    """

    # Report file written inside the watched directory.
    REPORT_FILENAME = ".vetinari/watch_report.jsonl"

    def __init__(self, config: WatchConfig | None = None):
        self._config = config or WatchConfig()
        self._watcher = FileWatcher(self._config)
        self._scanner = DirectiveScanner(self._config.max_file_size)
        self._directive_handlers: dict[str, Callable] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=500)
        # In-memory queue of DirectiveReport instances for external consumers.
        self.task_queue: list[DirectiveReport] = []
        self._queue_lock = threading.Lock()

        # D3 fix: tracks the last-dispatched normalized directive text per
        # (file_path, line_number) key so unchanged directives are not
        # re-dispatched on every subsequent save of the same file.
        self._last_dispatched: dict[tuple[str, int], str] = {}

        # Ensure the report directory exists (best-effort; non-fatal).
        self._report_path = Path(self._config.watch_dir) / self.REPORT_FILENAME
        self._report_path.parent.mkdir(parents=True, exist_ok=True)

        # Register default handlers
        self._register_defaults()

        # Wire up change callback
        self._watcher.on_change(self._handle_changes)

    def _register_defaults(self) -> None:
        """Register default directive handlers."""
        self._directive_handlers = {
            "fix": self._handle_fix,
            "add": self._handle_add,
            "test": self._handle_test,
            "explain": self._handle_explain,
            "refactor": self._handle_refactor,
            "review": self._handle_review,
        }

    def register_handler(self, action: str, handler: Callable) -> None:
        """Register a custom directive handler.

        Args:
            action: The directive action keyword (case-insensitive).
            handler: Callable that accepts a single ``VetinariDirective`` argument.
        """
        self._directive_handlers[action.lower()] = handler

    def start(self) -> None:
        """Start watch mode, beginning background file monitoring."""
        logger.info("Watch mode started: monitoring %s", self._config.watch_dir)
        self._watcher.start()

    def stop(self) -> None:
        """Stop watch mode and shut down the background monitoring thread."""
        self._watcher.stop()
        logger.info("Watch mode stopped")

    @property
    def is_running(self) -> bool:
        """Return True if the background watcher thread is active."""
        return self._watcher.is_running

    def drain_queue(self) -> list[DirectiveReport]:
        """Remove and return all pending directive reports from the queue.

        Returns:
            A list of all pending ``DirectiveReport`` instances, oldest first.
            The queue is empty after this call.
        """
        with self._queue_lock:
            items = list(self.task_queue)
            self.task_queue.clear()
        return items

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _record(self, report: DirectiveReport) -> None:
        """Write *report* durably then enqueue it for in-process consumers.

        D4 fix: the report file is written FIRST. Only if that succeeds is the
        report appended to ``task_queue``. A failed write does NOT enqueue, so
        callers never believe a directive was safely recorded when it was not.

        Args:
            report: The ``DirectiveReport`` to persist.
        """
        row = report.to_dict()
        row["target"] = _privacy_safe_report_text(row.get("target"))
        row["full_line"] = _privacy_safe_report_text(row.get("full_line"))
        row[PRIVACY_ENVELOPE_KEY] = privacy_receipt(
            privacy_class="subject_data",
            subject_id=report.file_path,
            source="watch.directive_report",
            erasure_token=f"watch.directive_report:{report.file_path}:{report.line_number}",
            redaction_applied=True,
        )
        require_privacy_envelope(row)
        try:
            with Path(self._report_path).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError as exc:
            logger.warning(
                "Could not write watch report for %s:%d — directive not enqueued: %s",
                report.file_path,
                report.line_number,
                exc,
            )
            return

        with self._queue_lock:
            self.task_queue.append(report)

    def _handle_changes(self, changes: list[FileChange]) -> None:
        """Process a batch of file changes, scanning for directives.

        Args:
            changes: List of ``FileChange`` objects detected since last poll.
        """
        event: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "changes": [{"path": c.path, "type": c.change_type} for c in changes],
            "directives": [],
        }

        if self._config.scan_directives:
            directives = self._scanner.scan_changes(changes)
            for d in directives:
                event["directives"].append({
                    "file": d.file_path,
                    "line": d.line_number,
                    "action": d.action,
                    "target": d.target,
                })
                self._dispatch_directive(d)

        self._history.append(event)

    def _dispatch_directive(self, directive: VetinariDirective) -> None:
        """Look up and invoke the handler registered for *directive.action*.

        D3 fix: before dispatching, compares the directive's full_line against
        the last-dispatched text for the same (file_path, line_number) key.
        If the content is unchanged the directive is silently skipped, preventing
        duplicate report rows and queue entries on subsequent saves of the same file.

        Args:
            directive: The parsed ``VetinariDirective`` to dispatch.
        """
        dedup_key = (directive.file_path, directive.line_number)
        normalized = directive.full_line.strip()
        if self._last_dispatched.get(dedup_key) == normalized:
            logger.debug(
                "Skipping unchanged directive at %s:%d — already dispatched",
                directive.file_path,
                directive.line_number,
            )
            return
        self._last_dispatched[dedup_key] = normalized

        handler = self._directive_handlers.get(directive.action)
        if handler:
            try:
                handler(directive)
            except Exception as e:
                logger.error("Directive handler error for '%s': %s", directive.action, e)
        else:
            logger.warning(
                "Unknown @vetinari directive action: '%s' in %s:%d",
                directive.action,
                directive.file_path,
                directive.line_number,
            )

    # ── Directive handlers ───────────────────────────────────────────────────

    def _handle_fix(self, d: VetinariDirective) -> None:
        """Handle a ``@vetinari fix`` directive.

        Logs the issue at WARNING level (it represents broken or problematic
        code that needs attention) and enqueues a high-priority report entry.

        Args:
            d: The parsed directive containing file, line, and target details.
        """
        logger.warning(
            "[watch] FIX needed — %s (line %d in %s)",
            d.target or d.full_line,
            d.line_number,
            d.file_path,
        )
        report = DirectiveReport(
            action="fix",
            file_path=d.file_path,
            line_number=d.line_number,
            target=d.target,
            full_line=d.full_line,
            priority="high",
        )
        self._record(report)

    def _handle_add(self, d: VetinariDirective) -> None:
        """Handle a ``@vetinari add`` directive.

        Logs that something should be added and enqueues a normal-priority
        report entry.

        Args:
            d: The parsed directive.
        """
        logger.info(
            "[watch] ADD requested — %s (line %d in %s)",
            d.target or d.full_line,
            d.line_number,
            d.file_path,
        )
        report = DirectiveReport(
            action="add",
            file_path=d.file_path,
            line_number=d.line_number,
            target=d.target,
            full_line=d.full_line,
            priority="normal",
        )
        self._record(report)

    def _handle_test(self, d: VetinariDirective) -> None:
        """Handle a ``@vetinari test`` directive.

        Flags the file or function for test coverage.  Logs at WARNING level
        because missing test coverage is a quality gap, and enqueues a report
        entry that CI tooling can consume.

        Args:
            d: The parsed directive.
        """
        target_desc = d.target or d.file_path
        logger.warning(
            "[watch] TEST COVERAGE needed — %s (line %d in %s)",
            target_desc,
            d.line_number,
            d.file_path,
        )
        report = DirectiveReport(
            action="test",
            file_path=d.file_path,
            line_number=d.line_number,
            target=d.target,
            full_line=d.full_line,
            priority="normal",
        )
        self._record(report)

    def _handle_explain(self, d: VetinariDirective) -> None:
        """Handle a ``@vetinari explain`` directive."""
        self._record_normal("explain", d, logging.INFO)

    def _handle_refactor(self, d: VetinariDirective) -> None:
        """Handle a ``@vetinari refactor`` directive."""
        self._record_normal("refactor", d, logging.INFO)

    def _handle_review(self, d: VetinariDirective) -> None:
        """Handle a ``@vetinari review`` directive."""
        self._record_normal("review", d, logging.WARNING)

    def _record_normal(self, action: str, d: VetinariDirective, level: int) -> None:
        logger.log(level, "[watch] %s: %s:%d - %s", action.upper(), d.file_path, d.line_number, d.target or d.full_line)
        report = DirectiveReport(
            action=action,
            file_path=d.file_path,
            line_number=d.line_number,
            target=d.target,
            full_line=d.full_line,
            priority="normal",
        )
        self._record(report)

    def get_history(self) -> list[dict[str, Any]]:
        """Return a snapshot of all watch events processed so far.

        Returns:
            A list of event dictionaries, each containing ``timestamp``,
            ``changes``, and ``directives`` keys.
        """
        return list(self._history)
