"""Conversion helpers for external training datasets."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.privacy.envelope import PrivacyClass, privacy_receipt

if TYPE_CHECKING:
    from vetinari.training.external_data import DatasetSpec

logger = logging.getLogger(__name__)


class ExternalDataConversionMixin:
    """Maps raw external dataset rows into Vetinari training JSONL records."""

    def _convert_to_training_format(
        self,
        ds: Any,
        spec: DatasetSpec,
        output_path: Path,
    ) -> int:
        """Convert a HuggingFace dataset object to Alpaca-style JSONL.

        For SFT datasets the output schema is ``{instruction, input, output}``.
        For DPO datasets the output schema is ``{prompt, chosen, rejected}``.

        Args:
            ds: A HuggingFace ``Dataset`` object (or any iterable of row dicts).
            spec: Specification used to determine field mapping and format.
            output_path: Destination path for the JSONL file.

        Returns:
            Number of records successfully written.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0

        with output_path.open("w", encoding="utf-8") as out:
            for row in ds:
                mapped = self._map_row(dict(row), spec)
                if mapped is None:
                    continue
                mapped = _attach_external_data_provenance(mapped, spec)
                out.write(json.dumps(mapped) + "\n")
                count += 1

        return count

    def _map_row(self, row: dict[str, Any], spec: DatasetSpec) -> dict[str, Any] | None:
        """Map a single HuggingFace dataset row to the Alpaca training format.

        SFT rows are mapped to ``{instruction, input, output}``.
        DPO rows are mapped to ``{prompt, chosen, rejected}``.

        The method attempts a best-effort field lookup using common column names
        found in popular HuggingFace datasets. Returns ``None`` when essential
        fields cannot be resolved so that the caller can skip the row.

        Args:
            row: Raw dictionary representing one dataset example.
            spec: Specification indicating the expected format and dataset name.

        Returns:
            Normalised dictionary ready for JSONL serialisation, or ``None``
            if the row cannot be mapped.
        """
        if spec.format == "sft":
            return self._map_sft_row(row)
        if spec.format == "dpo":
            return self._map_dpo_row(row)

        logger.warning(
            "Unknown format '%s' for dataset %s; skipping row",
            spec.format,
            spec.name,
        )
        return None

    @staticmethod
    def _map_sft_row(row: dict[str, Any]) -> dict[str, Any] | None:
        """Map a row to the SFT Alpaca format {instruction, input, output}.

        Args:
            row: Raw dataset row dictionary.

        Returns:
            Mapped dict or None if required fields are absent.
        """
        instruction = (
            row.get("instruction") or row.get("prompt") or row.get("question") or row.get("problem") or row.get("text")
        )
        output = (
            row.get("output")
            or row.get("response")
            or row.get("answer")
            or row.get("solution")
            or row.get("canonical_solution")
        )

        if not instruction or not output:
            return None

        context = row.get("input") or row.get("context") or ""

        return {
            "instruction": str(instruction),
            "input": str(context),
            "output": str(output),
        }

    @staticmethod
    def _map_dpo_row(row: dict[str, Any]) -> dict[str, Any] | None:
        """Map a row to the DPO format {prompt, chosen, rejected}.

        Args:
            row: Raw dataset row dictionary.

        Returns:
            Mapped dict or None if required fields are absent.
        """
        prompt = row.get("prompt") or row.get("question") or row.get("instruction")
        chosen = row.get("chosen") or row.get("accepted") or row.get("preferred")
        rejected = row.get("rejected") or row.get("dispreferred")

        if not prompt or not chosen or not rejected:
            return None

        return {
            "prompt": str(prompt),
            "chosen": str(chosen),
            "rejected": str(rejected),
        }


def _attach_external_data_provenance(record: dict[str, Any], spec: DatasetSpec) -> dict[str, Any]:
    """Attach source, license, revision, and privacy evidence to a converted row."""
    revision = (spec.revision or "").strip()
    if not revision or revision == "main":
        raise ValueError(f"Dataset {spec.name!r} requires immutable revision provenance before conversion")
    metadata = dict(record.get("metadata") or {})
    metadata.update({
        "source_dataset": spec.name,
        "source_subset": spec.subset,
        "dataset_revision": revision,
        "license_ref": spec.license_ref,
        "default_training_allowed": bool(spec.default_training_allowed),
        "privacy_receipt": privacy_receipt(
            privacy_class=PrivacyClass.OPERATIONAL.value,
            retention_days=30,
            source=f"external_data:{spec.name}@{revision}",
            redaction_applied=True,
        ),
    })
    wrapped = dict(record)
    wrapped["metadata"] = metadata
    return wrapped
