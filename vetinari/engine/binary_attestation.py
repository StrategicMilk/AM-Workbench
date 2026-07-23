"""GitHub/Sigstore provenance verification for AM Engine artifacts."""

from __future__ import annotations

import hmac
import json
import re
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import urlparse

import httpx

from vetinari.engine.binary_manifest import ENGINE_RELEASE_REPOSITORY, PINNED_RELEASE, sha256_file
from vetinari.exceptions import EngineBinaryCorruptError, EngineBinaryMissingError

_API_HOST = "api.github.com"
_GITHUB_API_VERSION = "2026-03-10"
_ATTESTATION_PREDICATE = "https://slsa.dev/provenance/v1"
_ATTESTATION_PAYLOAD_TYPE = "application/vnd.in-toto+json"
_IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
_WORKFLOW_PATH = ".github/workflows/engine.yml"
_GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_GITHUB_HOSTED_RUNNER = "https://github.com/actions/runner/github-hosted"
_MAX_ATTESTATION_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ATTESTATION_BUNDLES = 30
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _attestation_api_url(subject_sha256: str) -> str:
    owner, repository = ENGINE_RELEASE_REPOSITORY.split("/", maxsplit=1)
    return (
        f"https://api.github.com/repos/{owner}/{repository}/attestations/sha256:{subject_sha256}"
        f"?predicate_type={_ATTESTATION_PREDICATE}&per_page=100"
    )


def _fetch_github_attestation_bundles(subject_sha256: str, timeout_seconds: float) -> list[dict[str, object]]:
    """Fetch all provenance bundles for a release subject from GitHub."""
    if _SHA256_PATTERN.fullmatch(subject_sha256) is None:
        raise EngineBinaryCorruptError("cannot fetch an attestation for an invalid subject digest")
    url = _attestation_api_url(subject_sha256)
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.hostname != _API_HOST:
        raise EngineBinaryCorruptError("engine attestation API URL is not trusted", host=parsed_url.hostname)
    try:
        response = httpx.get(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                "User-Agent": "Vetinari-engine-provisioner",
            },
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise EngineBinaryMissingError(
            "unable to read the pinned AM Engine artifact attestation",
            subject_sha256=subject_sha256,
        ) from exc
    try:
        declared_size = response.headers.get("Content-Length")
        if declared_size is not None:
            try:
                declared_size_bytes = int(declared_size)
            except ValueError as exc:
                raise EngineBinaryCorruptError("GitHub attestation response has an invalid declared size") from exc
            if declared_size_bytes > _MAX_ATTESTATION_RESPONSE_BYTES:
                raise EngineBinaryCorruptError("GitHub attestation response exceeds its declared size budget")
        if len(response.content) > _MAX_ATTESTATION_RESPONSE_BYTES:
            raise EngineBinaryCorruptError("GitHub attestation response exceeds its size budget")
        if 'rel="next"' in response.headers.get("Link", ""):
            raise EngineBinaryCorruptError("GitHub attestation response exceeds one bounded page")
        payload = response.json()
    except EngineBinaryCorruptError:
        raise
    except ValueError as exc:
        raise EngineBinaryCorruptError("GitHub attestation response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EngineBinaryCorruptError("GitHub attestation response is not a JSON object")
    rows = payload.get("attestations")
    if not isinstance(rows, list) or not rows or len(rows) > _MAX_ATTESTATION_BUNDLES:
        raise EngineBinaryCorruptError("GitHub attestation response has no bounded attestation set")
    bundles: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("bundle"), dict):
            raise EngineBinaryCorruptError("GitHub attestation response contains a malformed row", index=index)
        bundle = row["bundle"]
        if len(json.dumps(bundle, separators=(",", ":"))) > _MAX_ATTESTATION_RESPONSE_BYTES:
            raise EngineBinaryCorruptError("GitHub attestation response contains an oversized bundle", index=index)
        bundles.append(bundle)
    return bundles


def _verify_slsa_statement(
    payload: bytes,
    *,
    subject_name: str,
    subject_sha256: str,
    repository: str,
    workflow_path: str,
    source_ref: str,
    source_commit: str,
) -> None:
    try:
        statement = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EngineBinaryCorruptError("AM Engine attestation payload is not valid JSON") from exc
    if not isinstance(statement, dict):
        raise EngineBinaryCorruptError("AM Engine attestation statement is not an object")
    if statement.get("_type") != _IN_TOTO_STATEMENT_TYPE or statement.get("predicateType") != _ATTESTATION_PREDICATE:
        raise EngineBinaryCorruptError("AM Engine attestation has an unexpected statement type")
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or not subjects:
        raise EngineBinaryCorruptError("AM Engine attestation has no subjects")
    matching_subjects = []
    for index, subject in enumerate(subjects):
        if not isinstance(subject, dict) or not isinstance(subject.get("name"), str):
            raise EngineBinaryCorruptError("AM Engine attestation has a malformed subject", index=index)
        digest = subject.get("digest")
        if not isinstance(digest, dict) or set(digest) != {"sha256"}:
            raise EngineBinaryCorruptError("AM Engine attestation subject has an invalid digest", index=index)
        sha256 = digest["sha256"]
        if not isinstance(sha256, str) or _SHA256_PATTERN.fullmatch(sha256) is None:
            raise EngineBinaryCorruptError("AM Engine attestation subject has a malformed SHA-256", index=index)
        name = str(subject["name"])
        if name == subject_name and hmac.compare_digest(sha256, subject_sha256):
            matching_subjects.append(subject)
    if len(matching_subjects) != 1:
        raise EngineBinaryCorruptError(
            "AM Engine attestation does not select exactly one downloaded subject",
            subject=subject_name,
            match_count=len(matching_subjects),
        )
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        raise EngineBinaryCorruptError("AM Engine attestation has no SLSA predicate")
    build_definition = predicate.get("buildDefinition")
    run_details = predicate.get("runDetails")
    if not isinstance(build_definition, dict) or not isinstance(run_details, dict):
        raise EngineBinaryCorruptError("AM Engine attestation has an incomplete SLSA predicate")
    external = build_definition.get("externalParameters")
    workflow = external.get("workflow") if isinstance(external, dict) else None
    expected_repository_url = f"https://github.com/{repository}"
    if not isinstance(workflow, dict) or (
        workflow.get("repository") != expected_repository_url
        or str(workflow.get("path", "")).lstrip("/") != workflow_path
        or workflow.get("ref") != source_ref
    ):
        raise EngineBinaryCorruptError("AM Engine attestation has an unexpected workflow build definition")
    dependencies = build_definition.get("resolvedDependencies")
    expected_dependency_uri = f"git+{expected_repository_url}@{source_ref}"
    if not isinstance(dependencies, list) or not any(
        isinstance(dependency, dict)
        and dependency.get("uri") == expected_dependency_uri
        and dependency.get("digest") == {"gitCommit": source_commit}
        for dependency in dependencies
    ):
        raise EngineBinaryCorruptError("AM Engine attestation does not resolve the expected source commit")
    builder = run_details.get("builder")
    if not isinstance(builder, dict) or builder.get("id") != _GITHUB_HOSTED_RUNNER:
        raise EngineBinaryCorruptError("AM Engine attestation was not produced by a GitHub-hosted runner")


def _verify_attestation_bundle(
    bundle_payload: dict[str, object],
    *,
    subject_name: str,
    subject_sha256: str,
    repository: str,
    workflow_path: str,
    source_ref: str,
    source_commit: str,
    refresh_trust: bool = True,
) -> None:
    """Cryptographically verify one Sigstore DSSE bundle and its SLSA identity."""
    if find_spec("sigstore") is None:
        raise EngineBinaryCorruptError("Sigstore verification support is unavailable")
    from sigstore.errors import Error as SigstoreError
    from sigstore.models import Bundle
    from sigstore.verify import Verifier, policy

    signer = f"https://github.com/{repository}/{workflow_path}@{source_ref}"
    repository_url = f"https://github.com/{repository}"
    verification_policy = policy.AllOf([
        policy.OIDCIssuer(_GITHUB_OIDC_ISSUER),
        policy.OIDCBuildSignerURI(signer),
        policy.OIDCBuildSignerDigest(source_commit),
        policy.OIDCRunnerEnvironment("github-hosted"),
        policy.OIDCSourceRepositoryURI(repository_url),
        policy.OIDCSourceRepositoryDigest(source_commit),
        policy.OIDCSourceRepositoryRef(source_ref),
        policy.OIDCBuildConfigURI(signer),
        policy.OIDCBuildConfigDigest(source_commit),
    ])
    try:
        bundle = Bundle.from_json(json.dumps(bundle_payload, sort_keys=True, separators=(",", ":")))
    except (SigstoreError, TypeError, ValueError) as exc:
        raise EngineBinaryCorruptError("AM Engine artifact attestation bundle is malformed") from exc
    try:
        verifier = Verifier.production(offline=not refresh_trust)
    except (SigstoreError, OSError) as exc:
        raise EngineBinaryMissingError("Sigstore production trust material is unavailable") from exc
    try:
        payload_type, statement = verifier.verify_dsse(bundle, verification_policy)
    except SigstoreError as exc:
        raise EngineBinaryCorruptError("AM Engine artifact attestation failed cryptographic verification") from exc
    if payload_type != _ATTESTATION_PAYLOAD_TYPE:
        raise EngineBinaryCorruptError("AM Engine attestation has an unexpected DSSE payload type")
    _verify_slsa_statement(
        statement,
        subject_name=subject_name,
        subject_sha256=subject_sha256,
        repository=repository,
        workflow_path=workflow_path,
        source_ref=source_ref,
        source_commit=source_commit,
    )


def _verify_github_attestation(
    artifact_path: Path,
    *,
    source_commit: str,
    timeout_seconds: float,
) -> None:
    """Require one identity-bound GitHub/Sigstore provenance attestation."""
    subject_sha256 = sha256_file(artifact_path)
    bundles = _fetch_github_attestation_bundles(subject_sha256, timeout_seconds)
    source_ref = f"refs/tags/{PINNED_RELEASE}"
    failures: list[EngineBinaryCorruptError] = []
    for bundle in bundles:
        try:
            _verify_attestation_bundle(
                bundle,
                subject_name=artifact_path.name,
                subject_sha256=subject_sha256,
                repository=ENGINE_RELEASE_REPOSITORY,
                workflow_path=_WORKFLOW_PATH,
                source_ref=source_ref,
                source_commit=source_commit,
            )
        except EngineBinaryCorruptError as exc:
            failures.append(exc)
        else:
            return
    raise EngineBinaryCorruptError(
        "no AM Engine attestation satisfies the pinned cryptographic identity policy",
        subject=artifact_path.name,
        rejected_attestations=len(failures),
    ) from failures[-1]
