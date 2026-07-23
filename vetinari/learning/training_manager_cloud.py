"""Cloud-training and lifecycle helpers for TrainingManager."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.constants import THREAD_JOIN_TIMEOUT

if TYPE_CHECKING:
    from .training_manager import TrainingDataset, TrainingJob


class TrainingManagerCloudMixin:
    """Unsupported cloud-training boundary and shutdown helpers."""

    if TYPE_CHECKING:
        _upsert_job: Any

    def train_cloud(
        self,
        model_id: str,
        dataset: TrainingDataset,
        provider: str = "huggingface",
        config: dict[str, Any] | None = None,
    ) -> TrainingJob:
        """Record a cloud fine-tuning request as an unsupported failed job.

        Returns a :class:`TrainingJob` in ``"failed"`` status.  Full API
        integration is an optional feature — if the provider SDK is not
        configured, the job result explains the unsupported provider boundary.

        Parameters
        ----------
        model_id:
            Base model identifier.
        dataset:
            Prepared dataset from :meth:`prepare_training_data`.
        provider:
            ``"huggingface"`` (default) or another provider name.
        config:
            Optional provider-specific configuration overrides.

        Args:
            model_id: The model id.
            dataset: The dataset.
            provider: The provider.
            config: The config.

        Returns:
            The TrainingJob result.

        """
        from .training_manager import TrainingJob, TrainingResult

        created_at = datetime.now(timezone.utc).isoformat()
        job_id = f"cloud-unsupported-{provider.strip().lower() or 'unknown'}-{int(time.monotonic() * 1000)}"
        result = TrainingResult(
            success=False,
            model_path=None,
            metrics={
                "training_examples": len(dataset.records),
                "dataset_format": dataset.format,
                "provider_configured": False,
            },
            duration_seconds=0.0,
            error=(
                f"Cloud training via '{provider}' has no configured provider integration. "
                "Use train_local() for on-device fine-tuning with QLoRA/DoRA."
            ),
        )
        job = TrainingJob(
            job_id=job_id,
            status="failed",
            provider=provider,
            model_id=model_id,
            created_at=created_at,
            progress=1.0,
            result=result,
        )
        self._upsert_job(job)
        return job

    def shutdown(self, timeout: float = THREAD_JOIN_TIMEOUT) -> None:
        """Flush any manager-owned training collector before app shutdown."""
        collector = getattr(self, "_collector_instance", None)
        if collector is not None and hasattr(collector, "shutdown"):
            collector.shutdown(timeout=timeout)

    def close(self, timeout: float = THREAD_JOIN_TIMEOUT) -> None:
        """Alias for shutdown() for resource-style lifecycle callers."""
        self.shutdown(timeout=timeout)
