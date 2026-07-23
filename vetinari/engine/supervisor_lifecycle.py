"""Owned-process lifecycle operations for :mod:`vetinari.engine.supervisor`.

This mixin isolates child-process mutation from protocol and event ingestion.
The public ``EngineSupervisor`` remains the sole owner of lifecycle state.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from contextlib import suppress
from pathlib import Path
from typing import TextIO, cast

from vetinari.engine import supervisor as supervisor_types
from vetinari.exceptions import (
    EngineBinaryMissingError,
    EngineError,
    EngineUnavailableError,
    EngineVersionMismatchError,
)
from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


def _supervisor_surface():  # type: ignore[no-untyped-def]
    """Return the public module so supported monkeypatch seams stay stable."""
    from vetinari.engine import supervisor

    return supervisor


class EngineLifecycleMixin:
    """Manage provisioning, owned child generations, and restart policy."""

    def provision(self, *, consent: bool, accelerator: str | None = None) -> Path:
        """Provision the pinned engine bundle only after explicit operator consent.

        Returns:
            Path to the atomically installed executable.

        Raises:
            EngineError: If authorization, verification, or activation fails.
        """
        surface = _supervisor_surface()
        if consent is not True:
            raise EngineUnavailableError(
                "AM Engine provisioning requires explicit operator consent",
                operation="provision",
            )
        with self._lock:
            if self._provision_in_progress:
                raise EngineUnavailableError("AM Engine provisioning is already in progress")
            if self._state in {surface.EngineState.STARTING, surface.EngineState.DRAINING}:
                raise EngineUnavailableError(
                    f"AM Engine provisioning is refused during the {self._state.value} lifecycle transition"
                )
            recorded_owner = self._endpoint
            if recorded_owner is None and self.pidfile_path.exists():
                recorded_owner = self._read_pidfile(strict=True)
            if (
                self._process is not None
                or self._endpoint is not None
                or (recorded_owner is not None and self._pid_alive(recorded_owner.pid))
            ):
                raise EngineUnavailableError("AM Engine provisioning is refused while an owner is active")
            self._provision_in_progress = True
            self._state = surface.EngineState.PROVISIONING
            self._user_message = "AM Engine provisioning is in progress"
        try:
            installed = surface.provision_binary(accelerator=accelerator)
        except EngineBinaryMissingError as exc:
            if "source commit pin" in str(exc):
                message = (
                    "AM Engine provisioning is blocked until the immutable release commit pin "
                    "is published in a Vetinari package update"
                )
                with self._lock:
                    self._provision_in_progress = False
                    self._state = surface.EngineState.DEGRADED
                    self._user_message = message
                raise EngineBinaryMissingError(message, prerequisite="PINNED_RELEASE_COMMIT") from exc
            with self._lock:
                self._provision_in_progress = False
                self._state = surface.EngineState.DEGRADED
                self._user_message = "AM Engine provisioning source is unavailable"
            raise
        except EngineError:
            with self._lock:
                self._provision_in_progress = False
                self._state = surface.EngineState.DEGRADED
                self._user_message = "AM Engine provisioning failed; inspect the typed provisioning error"
            raise
        except Exception:
            with self._lock:
                self._provision_in_progress = False
                self._state = surface.EngineState.DEGRADED
                self._user_message = "AM Engine provisioning failed unexpectedly; inspect the provisioning diagnostics"
            raise
        with self._lock:
            self._provision_in_progress = False
            self._state = surface.EngineState.STOPPED
            self._user_message = "AM Engine provisioning completed; the runtime remains stopped until requested"
        return cast(Path, installed)

    def ensure_running(self) -> supervisor_types.EngineEndpoint:
        """Return a healthy endpoint, adopting a live owner or starting on demand.

        Returns:
            Handshake-verified connection metadata.

        Raises:
            EngineError: If discovery, start, or readiness fails closed.
        """
        surface = _supervisor_surface()
        with self._lock:
            if self._provision_in_progress:
                raise EngineUnavailableError(
                    "AM Engine provisioning is in progress; wait for it to complete before starting"
                )
            if self._suspended:
                raise EngineUnavailableError("AM Engine starts are suspended")
            if self._restart_exhausted:
                raise EngineUnavailableError(
                    "AM Engine automatic restart attempts are exhausted; repair the failure and call restart()"
                )
            if self._endpoint is not None and self._handshake(self._endpoint, raise_on_mismatch=True):
                self._state = surface.EngineState.RUNNING
                self._user_message = ""
                return self._endpoint
            adopted = self._adopt_pidfile_owner()
            if adopted is not None:
                return adopted
            return self._start_child()

    def status(self) -> supervisor_types.EngineStatus:
        """Poll the known owner without spawning, downloading, or provisioning.

        Returns:
            Current read-only lifecycle snapshot.
        """
        surface = _supervisor_surface()
        with self._lock:
            endpoint = self._endpoint or self._read_pidfile()
            healthy = False
            observed_mismatch: EngineVersionMismatchError | None = None
            if endpoint is not None and self._pid_alive(endpoint.pid):
                try:
                    healthy = self._probe_endpoint(endpoint)
                except EngineVersionMismatchError as exc:
                    observed_mismatch = exc
            state = self._state
            user_message = self._user_message
            if observed_mismatch is not None:
                state = surface.EngineState.VERSION_MISMATCH
                user_message = str(observed_mismatch)
            elif healthy and state not in {surface.EngineState.DRAINING, surface.EngineState.PROVISIONING}:
                state = surface.EngineState.RUNNING
            elif state is surface.EngineState.RUNNING:
                state = surface.EngineState.DEGRADED
            with self._startup_log_lock:
                startup_log_tail = tuple(self._startup_log_tail)
            return surface.EngineStatus(
                state=state,
                healthy=healthy,
                endpoint=endpoint.url if endpoint else None,
                pid=endpoint.pid if endpoint else None,
                restart_attempts=self._restart_attempts,
                user_message=user_message,
                capabilities=self.capabilities,
                startup_log_tail=startup_log_tail,
            )

    def handle_process_exit(
        self,
        returncode: int | None = None,
        *,
        _expected_process: subprocess.Popen[str] | None = None,
    ) -> supervisor_types.EngineEndpoint | None:
        """Apply capped exponential restart policy after an unexpected exit.

        Returns:
            Restarted endpoint, or ``None`` if retries stop.
        """
        surface = _supervisor_surface()
        with self._lock:
            if _expected_process is not None and self._process is not _expected_process:
                return self._endpoint
            exited_generation = self._endpoint.generation if self._endpoint is not None else None
            if self._monitor_stop is not None:
                self._monitor_stop.set()
        self._stop_event_ingest()
        from vetinari.engine.client import invalidate_engine_client

        invalidate_engine_client(endpoint_generation=exited_generation)
        with self._lock:
            if _expected_process is not None and self._process is not _expected_process:
                return self._endpoint
            self._process = None
            self._endpoint = None
            if (
                self._process_started_at is not None
                and self._monotonic() - self._process_started_at >= self.config.restart_reset_seconds
            ):
                self._restart_attempts = 0
            self._process_started_at = None
            self._remove_runtime_records()
            if self._suspended:
                self._state = surface.EngineState.STOPPED
                return None
        while True:
            with self._lock:
                if self._suspended:
                    self._state = surface.EngineState.STOPPED
                    return None
                if self._restart_attempts >= self.config.max_restart_attempts:
                    self._restart_exhausted = True
                    self._state = surface.EngineState.DEGRADED
                    self._user_message = (
                        f"AM Engine stopped after {self.config.max_restart_attempts} restart attempts; "
                        f"last process exit was {returncode!r}; check the engine binary and logs"
                    )
                    return None
                delay = self.config.restart_base_delay_seconds * (2**self._restart_attempts)
                self._restart_attempts += 1
                attempt = self._restart_attempts
                self._state = surface.EngineState.STARTING
            self._sleep(delay)
            try:
                return self.ensure_running()
            except EngineError as exc:
                logger.warning("AM Engine restart attempt %s failed: %s", attempt, exc)

    def _start_exit_monitor(self, process: subprocess.Popen[str], endpoint: supervisor_types.EngineEndpoint) -> None:
        stop = threading.Event()

        def monitor() -> None:
            while not stop.wait(0.05):
                returncode = process.poll()
                if returncode is None:
                    continue
                self.handle_process_exit(returncode, _expected_process=process)
                return

        thread = threading.Thread(
            target=monitor,
            name=f"am-engine-exit-monitor-{endpoint.generation}",
            daemon=True,
        )
        with self._lock:
            previous = self._monitor_stop
            self._monitor_stop = stop
            self._monitor_thread = thread
        if previous is not None:
            previous.set()
        thread.start()

    def drain(self) -> None:
        """Stop an owned child with bounded terminate-then-kill escalation."""
        with self._lifecycle_transition_lock:
            self._drain_owned_generation()

    def _drain_owned_generation(self) -> None:
        surface = _supervisor_surface()
        with self._lock:
            process = self._process
            owns_process = process is not None
            self._suspended = True
            self._state = surface.EngineState.DRAINING
            if self._monitor_stop is not None:
                self._monitor_stop.set()
        self._stop_event_ingest()
        if process is not None:
            try:
                self._terminate_child(process)
            except (OSError, subprocess.TimeoutExpired) as exc:
                with self._lock:
                    self._state = surface.EngineState.DEGRADED
                    self._user_message = "AM Engine did not stop before the drain deadline"
                raise EngineUnavailableError(self._user_message, pid=process.pid) from exc
        with self._lock:
            self._process = None
            self._endpoint = None
            self._process_started_at = None
            self._state = surface.EngineState.STOPPED
            if owns_process:
                self._remove_runtime_records()

    def _terminate_child(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=self.config.drain_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self.config.drain_timeout_seconds)

    def resume(self) -> None:
        """Allow subsequent on-demand starts after a drain or explicit suspension."""
        with self._lifecycle_transition_lock, self._lock:
            self._suspended = False

    def restart(self) -> supervisor_types.EngineEndpoint:
        """Replace a live owned child or recover after automatic restarts are exhausted.

        Returns:
            Handshake-verified metadata for a newer generation.
        """
        with self._restart_condition:
            observed_restart_epoch = self._restart_epoch
            while self._restart_in_progress and self._restart_epoch == observed_restart_epoch:
                self._restart_condition.wait()
            if self._restart_epoch != observed_restart_epoch:
                return self.ensure_running()
            self._restart_in_progress = True
        succeeded = False
        try:
            endpoint = self._restart_owned_generation()
            succeeded = True
            return endpoint
        finally:
            with self._restart_condition:
                if succeeded:
                    self._restart_epoch += 1
                self._restart_in_progress = False
                self._restart_condition.notify_all()

    def _restart_owned_generation(self) -> supervisor_types.EngineEndpoint:
        with self._lifecycle_transition_lock:
            return self._restart_owned_generation_locked()

    def _restart_owned_generation_locked(self) -> supervisor_types.EngineEndpoint:
        surface = _supervisor_surface()
        with self._lock:
            if self._provision_in_progress:
                raise EngineUnavailableError(
                    "controlled restart is refused while AM Engine provisioning is in progress"
                )
            recovering = self._restart_exhausted and self._process is None and self._endpoint is None
            if not recovering and (self._process is None or self._endpoint is None):
                raise EngineUnavailableError("controlled restart requires an engine child owned by this supervisor")
            previous_generation = self._endpoint_generation
            if recovering:
                self._restart_attempts = 0
                self._restart_exhausted = False
            self._user_message = "AM Engine controlled restart is in progress"
        if not recovering:
            self.drain()
            self.resume()
        try:
            endpoint = self.ensure_running()
        except EngineError:
            with self._lock:
                if recovering:
                    self._restart_attempts = self.config.max_restart_attempts
                    self._restart_exhausted = True
                self._state = surface.EngineState.DEGRADED
                self._user_message = "AM Engine controlled restart failed; inspect startup diagnostics"
            raise
        if endpoint.generation <= previous_generation:
            with self._lock:
                self._state = surface.EngineState.DEGRADED
                self._user_message = "AM Engine controlled restart did not create a fresh generation"
            raise EngineUnavailableError(
                self._user_message,
                previous_generation=previous_generation,
                observed_generation=endpoint.generation,
            )
        with self._lock:
            self._restart_attempts = 0
            self._restart_exhausted = False
            self._user_message = ""
        return endpoint

    def suspend(self) -> None:
        """Prevent new starts without killing a shared owner."""
        with self._lock:
            self._suspended = True

    def _resolve_configured_binary(self) -> Path:
        if self.config.binary_path is not None:
            path = self.config.binary_path.expanduser().resolve()
            if not path.is_file():
                raise EngineUnavailableError("configured AM Engine binary is missing", path=str(path))
            return path
        return cast(Path, _supervisor_surface().resolve_binary())

    def _version_mismatch_error(self, observed: str, *, action: str) -> EngineVersionMismatchError:
        message = f"AM Engine version mismatch: expected {self.config.expected_version}; {action}"
        return EngineVersionMismatchError(message, expected=self.config.expected_version, observed=observed)

    def _record_version_mismatch(self, error: EngineVersionMismatchError) -> None:
        self._state = _supervisor_surface().EngineState.VERSION_MISMATCH
        self._user_message = str(error).split(" [", maxsplit=1)[0]

    def _start_child(self) -> supervisor_types.EngineEndpoint:
        surface = _supervisor_surface()
        binary_path = self._binary_resolver().resolve(strict=True)
        receipt_anchor = self._prepare_receipt_trust()
        verified_version = surface.probe_version(binary_path)
        if verified_version != self.config.expected_version:
            mismatch = self._version_mismatch_error(verified_version, action="reinstall the pinned release")
            self._record_version_mismatch(mismatch)
            raise mismatch
        model_args: list[str] = []
        if self.config.model_path is not None:
            model_path = self.config.model_path.expanduser().resolve()
            if model_path.suffix.lower() != ".gguf" or not model_path.is_file():
                raise EngineUnavailableError("configured AM Engine model must be an existing regular .gguf file")
            if self.config.runtime_mode is surface.EngineRuntimeMode.SCAFFOLD:
                model_args = ["--model", str(model_path)]
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            surface._secure_private_path(self.runtime_dir, directory=True)
        except OSError as exc:
            self._state = surface.EngineState.DEGRADED
            self._user_message = "AM Engine runtime directory could not be secured"
            raise EngineUnavailableError(self._user_message, path=str(self.runtime_dir)) from exc
        port = self.config.port or surface._reserve_ephemeral_port(self.config.host)
        token = surface.secrets.token_urlsafe(32)
        surface._write_private_text(self.token_path, token)
        self._write_auth_policy(token)
        surface.write_engine_config(
            self.config,
            self.config_path,
            port=port,
            auth_token_path=self.token_path.resolve(),
            auth_policy_path=self.auth_policy_path.resolve(),
        )
        if self.config.runtime_mode is surface.EngineRuntimeMode.OWNED:
            argv = [
                str(binary_path),
                "--config",
                str(self.config_path.resolve()),
                "--host",
                self.config.host,
                "--port",
                str(port),
                *self.config.extra_args,
            ]
        else:
            argv = [
                str(binary_path),
                "--host",
                self.config.host,
                "--port",
                str(port),
                "--api-key-file",
                str(self.token_path.resolve()),
                "--ctx-size",
                str(self.config.context_size),
                "--gpu-layers",
                str(self.config.gpu_layers),
                "--parallel",
                str(self.config.parallel_requests),
                *self.config.extra_args,
                *model_args,
            ]
        self._state = surface.EngineState.STARTING
        self._clear_startup_logs()
        child_environment = {**os.environ, "VETINARI_ENGINE_TOKEN_FILE": str(self.token_path)}
        for variable in (
            "VETINARI_ENGINE_RECEIPT_TRUST_ANCHOR",
            "VETINARI_ENGINE_RECEIPT_LEDGER",
            "VETINARI_ENGINE_RECEIPT_ANCHOR_SHA256",
            "VETINARI_ENGINE_RECEIPT_AUTHORITY_PIN_SHA256",
            "VETINARI_ENGINE_RECEIPT_KEY_SERVICE",
        ):
            child_environment.pop(variable, None)
        if receipt_anchor is not None:
            receipt_trust_anchor_path = self.config.receipt_trust_anchor_path
            receipt_ledger_path = self.config.receipt_ledger_path
            receipt_anchor_sha256 = self.config.receipt_anchor_sha256
            receipt_authority_pin_sha256 = self.config.receipt_authority_pin_sha256
            assert receipt_trust_anchor_path is not None
            assert receipt_ledger_path is not None
            assert receipt_anchor_sha256 is not None
            assert receipt_authority_pin_sha256 is not None
            child_environment.update({
                "VETINARI_ENGINE_RECEIPT_TRUST_ANCHOR": str(receipt_trust_anchor_path.resolve()),
                "VETINARI_ENGINE_RECEIPT_LEDGER": str(receipt_ledger_path.resolve()),
                "VETINARI_ENGINE_RECEIPT_ANCHOR_SHA256": receipt_anchor_sha256,
                "VETINARI_ENGINE_RECEIPT_AUTHORITY_PIN_SHA256": receipt_authority_pin_sha256,
            })
        try:
            process = self._process_factory(
                argv,
                cwd=str(binary_path.parent),
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=False,
            )
        except OSError as exc:
            self._state = surface.EngineState.DEGRADED
            self._user_message = "AM Engine could not be started; verify the installed binary"
            with suppress(OSError):
                self._remove_runtime_records()
            raise EngineUnavailableError(self._user_message, path=str(binary_path)) from exc
        self._capture_startup_output(process, auth_token=token)
        endpoint = surface.EngineEndpoint(
            process.pid, self.config.host, port, self.token_path, self._endpoint_generation + 1
        )
        self._process = process
        try:
            self._write_pidfile(endpoint, verified_version=verified_version)
            deadline = self._monotonic() + self.config.startup_timeout_seconds
            while self._monotonic() < deadline:
                if process.poll() is not None:
                    self._state = surface.EngineState.DEGRADED
                    self._user_message = "AM Engine exited before becoming ready"
                    raise EngineUnavailableError(self._user_message, returncode=process.returncode)
                if self._handshake(endpoint, raise_on_mismatch=True):
                    self._write_pidfile(endpoint, verified_version=verified_version)
                    self._endpoint_generation = endpoint.generation
                    self._endpoint = endpoint
                    self._state = surface.EngineState.RUNNING
                    self._restart_exhausted = False
                    self._process_started_at = self._monotonic()
                    self._user_message = ""
                    self._last_activity = self._monotonic()
                    self._start_exit_monitor(process, endpoint)
                    return endpoint
                self._sleep(0.05)
            self._state = surface.EngineState.DEGRADED
            self._user_message = "AM Engine did not become ready before the startup deadline"
            raise EngineUnavailableError(self._user_message)
        except Exception:
            try:
                self._terminate_child(process)
            except (OSError, subprocess.TimeoutExpired):
                logger.exception("AM Engine failed-start child could not be terminated cleanly")
            self._process = None
            self._endpoint = None
            self._process_started_at = None
            with suppress(OSError):
                self._remove_runtime_records()
            raise

    def _clear_startup_logs(self) -> None:
        with self._startup_log_lock:
            self._startup_log_epoch += 1
            self._startup_log_tail.clear()
            self._startup_log_chars = 0

    def _capture_startup_output(self, process: subprocess.Popen[str], *, auth_token: str) -> None:
        surface = _supervisor_surface()
        stream = cast(TextIO | None, getattr(process, "stdout", None))
        if stream is None:
            return
        with self._startup_log_lock:
            log_epoch = self._startup_log_epoch

        def consume() -> None:
            try:
                for raw_line in stream:
                    line = "".join(
                        character if character == "\t" or ord(character) >= 32 else "�"
                        for character in raw_line.rstrip("\r\n")
                    )
                    if not line:
                        continue
                    line = redact_text(line.replace(auth_token, "[REDACTED]"))
                    if len(line) > surface._STARTUP_LOG_LINE_MAX_CHARS:
                        line = f"{line[: surface._STARTUP_LOG_LINE_MAX_CHARS]}…"
                    with self._startup_log_lock:
                        if log_epoch != self._startup_log_epoch:
                            return
                        if len(self._startup_log_tail) == self._startup_log_tail.maxlen:
                            self._startup_log_chars -= len(self._startup_log_tail.popleft())
                        self._startup_log_tail.append(line)
                        self._startup_log_chars += len(line)
                        while self._startup_log_tail and self._startup_log_chars > surface._STARTUP_LOG_MAX_CHARS:
                            self._startup_log_chars -= len(self._startup_log_tail.popleft())
            except (OSError, UnicodeError):
                logger.warning("AM Engine startup log stream became unreadable", exc_info=True)
            finally:
                with suppress(OSError):
                    stream.close()

        thread = threading.Thread(target=consume, name="am-engine-startup-log", daemon=True)
        with self._startup_log_lock:
            self._startup_log_thread = thread
        thread.start()
