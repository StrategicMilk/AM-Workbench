#!/usr/bin/env python3
"""Fetch and verify the governed AM Engine native-test model and license."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
MODEL_URL = (
    "https://huggingface.co/tensorblock/tinyllama-15M-stories-GGUF/resolve/"
    "51b755181aac158c3ee689c0bd86f49a8291d1da/tinyllama-15M-stories-Q2_K.gguf"
)
MODEL_SHA256 = "f7e39dc9f26f3d39bf59e885349c6eec65880f685322d591f53e6cdb46ceb2e9"
MODEL_SIZE = 13_717_344
MODEL_MAX_BYTES = 16_777_216
LICENSE_URL = (
    "https://huggingface.co/tensorblock/tinyllama-15M-stories-GGUF/resolve/"
    "51b755181aac158c3ee689c0bd86f49a8291d1da/README.md"
)
LICENSE_SHA256 = "c8434895da38a8720e24712d2d79a0b4dfba77c94a5307ac974f44c194ad0af7"
LICENSE_MAX_BYTES = 131_072

# Apache-2.0 Qwen-owned coder fixture. The commit, GGUF digest, size, and
# license are immutable so CI cannot silently substitute an arbitrary model.
FIM_MODEL_REVISION = "ebb2015119c907b064c512bf053e945850b5875f"
FIM_MODEL_URL = (
    "https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/"
    f"{FIM_MODEL_REVISION}/qwen2.5-coder-0.5b-instruct-q2_k.gguf"
)
FIM_MODEL_SHA256 = "f9bddf294ef15c80bb64a2cdcf15d5b25caf88fb4f4a12383bc9f7a01a09c2e3"
FIM_MODEL_SIZE = 415_182_720
FIM_MODEL_MAX_BYTES = 419_430_400
FIM_LICENSE_URL = f"https://huggingface.co/Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF/resolve/{FIM_MODEL_REVISION}/LICENSE"
FIM_LICENSE_SHA256 = "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e"
FIM_LICENSE_MAX_BYTES = 131_072


@dataclass(frozen=True, slots=True)
class VerifiedDownload:
    """Verified bytes and their measured SHA-256 identity."""

    payload: bytes
    sha256: str


def download_verified(url: str, expected_sha256: str, maximum_bytes: int) -> VerifiedDownload:
    """Download one bounded HTTPS resource and verify its exact digest.

    Args:
        url: Commit-pinned HTTPS resource URL.
        expected_sha256: Required lowercase hexadecimal SHA-256.
        maximum_bytes: Maximum accepted response size.

    Returns:
        The verified bytes and measured digest.

    Raises:
        ValueError: If the URL, size, or digest violates the governed contract.
        RuntimeError: If the remote resource cannot be read.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "huggingface.co":
        raise ValueError("engine fixture evidence must use the governed Hugging Face HTTPS host")
    try:
        request = Request(url, headers={"User-Agent": "Vetinari-engine-fixture-evidence"})  # noqa: S310  # nosec B310 -- governed HTTPS host
        with urlopen(request, timeout=30) as response:  # noqa: S310  # nosec B310 -- governed HTTPS host
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > maximum_bytes:
                raise ValueError(f"engine fixture resource declares {declared} bytes above its budget")
            payload = response.read(maximum_bytes + 1)
    except (HTTPError, TimeoutError, URLError) as exc:
        raise RuntimeError(f"unable to download governed engine fixture resource {url}") from exc
    if not payload or len(payload) > maximum_bytes:
        raise ValueError("engine fixture resource is empty or exceeds its bounded download budget")
    observed = hashlib.sha256(payload).hexdigest()
    if observed != expected_sha256:
        raise ValueError(f"engine fixture resource SHA-256 mismatch: {observed}")
    return VerifiedDownload(payload=payload, sha256=observed)


def _matches_governed_file(path: Path, *, expected_sha256: str, expected_size: int | None = None) -> bool:
    """Return whether a cached file has the exact governed identity."""
    try:
        if not path.is_file() or (expected_size is not None and path.stat().st_size != expected_size):
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected_sha256
    except OSError:
        return False


def _cached_record(
    model_output: Path,
    evidence_directory: Path,
    *,
    model_url: str,
    model_sha256: str,
    model_size: int,
    license_filename: str,
    license_url: str,
    license_sha256: str,
    declared_license: str,
    revision: str | None = None,
    required_capability: str | None = None,
) -> dict[str, object] | None:
    """Load a complete, exact cached fixture record or reject the cache."""
    if not _matches_governed_file(
        model_output,
        expected_sha256=model_sha256,
        expected_size=model_size,
    ) or not _matches_governed_file(
        evidence_directory / license_filename,
        expected_sha256=license_sha256,
    ):
        return None
    evidence_path = evidence_directory / "evidence.json"
    try:
        record = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("schema_version") != "amw-engine-fixture-evidence.v1":
        return None
    model = record.get("model")
    license_record = record.get("license")
    if not isinstance(model, dict) or not isinstance(license_record, dict):
        return None
    expected_model: dict[str, object] = {
        "url": model_url,
        "sha256": model_sha256,
        "size_bytes": model_size,
    }
    if revision is not None:
        expected_model["revision"] = revision
    if required_capability is not None:
        expected_model["required_capability"] = required_capability
    expected_license = {
        "url": license_url,
        "sha256": license_sha256,
        "declared_license": declared_license,
        "size_bytes": (evidence_directory / license_filename).stat().st_size,
    }
    if model != expected_model or license_record != expected_license:
        return None
    return record


def ensure_fixture(model_output: Path, evidence_directory: Path, *, fixture: str = "native") -> dict[str, object]:
    """Reuse an exact governed cache or download and verify the selected fixture."""
    if fixture == "native":
        cached = _cached_record(
            model_output,
            evidence_directory,
            model_url=MODEL_URL,
            model_sha256=MODEL_SHA256,
            model_size=MODEL_SIZE,
            license_filename="README.md",
            license_url=LICENSE_URL,
            license_sha256=LICENSE_SHA256,
            declared_license="MIT",
        )
        return cached if cached is not None else verify_fixture(model_output, evidence_directory)
    if fixture == "fim":
        cached = _cached_record(
            model_output,
            evidence_directory,
            model_url=FIM_MODEL_URL,
            model_sha256=FIM_MODEL_SHA256,
            model_size=FIM_MODEL_SIZE,
            license_filename="LICENSE",
            license_url=FIM_LICENSE_URL,
            license_sha256=FIM_LICENSE_SHA256,
            declared_license="Apache-2.0",
            revision=FIM_MODEL_REVISION,
            required_capability="fill-in-the-middle",
        )
        return cached if cached is not None else verify_fim_fixture(model_output, evidence_directory)
    raise ValueError(f"unknown governed engine fixture: {fixture}")


def verify_fixture(model_output: Path, evidence_directory: Path) -> dict[str, object]:
    """Verify the governed model and license, then write a machine record.

    Args:
        model_output: Destination for verified GGUF bytes.
        evidence_directory: Destination for retained license and JSON evidence.

    Returns:
        The machine-readable verification record written to disk.

    Raises:
        ValueError: If content violates the governed identity or license contract.
        RuntimeError: If a governed resource cannot be downloaded.
        OSError: If verified evidence cannot be persisted.
    """
    model = download_verified(MODEL_URL, MODEL_SHA256, MODEL_MAX_BYTES)
    if len(model.payload) != MODEL_SIZE:
        raise ValueError(f"engine fixture model size mismatch: {len(model.payload)}")
    license_evidence = download_verified(LICENSE_URL, LICENSE_SHA256, LICENSE_MAX_BYTES)
    license_text = license_evidence.payload.decode("utf-8")
    frontmatter = license_text.split("---", maxsplit=2)
    if len(frontmatter) < 3 or "license: mit" not in frontmatter[1].lower():
        raise ValueError("engine fixture repository card does not declare the expected MIT license")
    model_output.parent.mkdir(parents=True, exist_ok=True)
    model_output.write_bytes(model.payload)
    evidence_directory.mkdir(parents=True, exist_ok=True)
    (evidence_directory / "README.md").write_bytes(license_evidence.payload)
    record: dict[str, object] = {
        "schema_version": "amw-engine-fixture-evidence.v1",
        "model": {"url": MODEL_URL, "sha256": model.sha256, "size_bytes": len(model.payload)},
        "license": {
            "url": LICENSE_URL,
            "sha256": license_evidence.sha256,
            "declared_license": "MIT",
            "size_bytes": len(license_evidence.payload),
        },
    }
    (evidence_directory / "evidence.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record


def verify_fim_fixture(model_output: Path, evidence_directory: Path) -> dict[str, object]:
    """Verify the governed FIM-capable model and Apache-2.0 evidence.

    Args:
        model_output: Destination for verified GGUF bytes.
        evidence_directory: Destination for retained license and JSON evidence.

    Returns:
        The machine-readable verification record written to disk.

    Raises:
        ValueError: If content violates the governed identity or license contract.
        RuntimeError: If a governed resource cannot be downloaded.
        OSError: If verified evidence cannot be persisted.
    """
    model = download_verified(FIM_MODEL_URL, FIM_MODEL_SHA256, FIM_MODEL_MAX_BYTES)
    if len(model.payload) != FIM_MODEL_SIZE:
        raise ValueError(f"engine FIM fixture model size mismatch: {len(model.payload)}")
    license_evidence = download_verified(
        FIM_LICENSE_URL,
        FIM_LICENSE_SHA256,
        FIM_LICENSE_MAX_BYTES,
    )
    license_text = license_evidence.payload.decode("utf-8")
    if "Apache License" not in license_text or "Version 2.0, January 2004" not in license_text:
        raise ValueError("engine FIM fixture evidence does not contain the expected Apache-2.0 license")
    model_output.parent.mkdir(parents=True, exist_ok=True)
    model_output.write_bytes(model.payload)
    evidence_directory.mkdir(parents=True, exist_ok=True)
    (evidence_directory / "LICENSE").write_bytes(license_evidence.payload)
    record: dict[str, object] = {
        "schema_version": "amw-engine-fixture-evidence.v1",
        "model": {
            "url": FIM_MODEL_URL,
            "revision": FIM_MODEL_REVISION,
            "sha256": model.sha256,
            "size_bytes": len(model.payload),
            "required_capability": "fill-in-the-middle",
        },
        "license": {
            "url": FIM_LICENSE_URL,
            "sha256": license_evidence.sha256,
            "declared_license": "Apache-2.0",
            "size_bytes": len(license_evidence.payload),
        },
    }
    (evidence_directory / "evidence.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return record


def build_parser() -> argparse.ArgumentParser:
    """Build the engine fixture verifier CLI parser.

    Returns:
        Configured argument parser.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        choices=("native", "fim"),
        default="native",
        help="Governed fixture contract to provision (default: native)",
    )
    parser.add_argument("--model-output", type=Path, required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run governed fixture verification and persist its evidence.

    Args:
        argv: Optional CLI arguments.

    Returns:
        Zero after successful verification.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.fixture == "fim":
        verify_fim_fixture(args.model_output, args.evidence_directory)
    else:
        verify_fixture(args.model_output, args.evidence_directory)
    LOGGER.info("verified governed engine %s fixture and license evidence", args.fixture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
