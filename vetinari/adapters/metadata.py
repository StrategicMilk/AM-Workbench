"""Model metadata path helpers."""

from __future__ import annotations

from pathlib import Path

from vetinari.security.fail_closed import PathTraversalError, sanitize_untrusted_text


def get_model_metadata_path(model_file: str) -> Path:
    """Return the metadata sidecar path for a model file.

    Args:
        model_file: Model file path.

    Returns:
        Metadata sidecar path.

    Raises:
        PathTraversalError: If the model path tries to traverse parents.
        UntrustedInputError: If the path is not safe text.
    """
    safe_model_file = sanitize_untrusted_text(model_file, max_length=2_000)
    path = Path(safe_model_file)
    if any(part == ".." for part in path.parts):
        raise PathTraversalError("model metadata sidecar path cannot traverse parent directories")
    return Path(f"{safe_model_file}.meta.json")


__all__ = ["get_model_metadata_path"]
