# noqa: VET124 - cross-module AM Engine release-attestation evidence
"""Known-bad and known-good AM Engine release attestation controls."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

import vetinari.engine.binary as binary_module
import vetinari.engine.binary_attestation as attestation_module
from vetinari.engine.binary import verify_release_manifest
from vetinari.exceptions import EngineBinaryCorruptError, EngineBinaryMissingError

_ENGINE_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "engine.yml"
_ENGINE_REFERENCE = Path(__file__).parents[1] / "docs" / "reference" / "engine.md"
_BUILD_SCRIPT = Path(__file__).parents[1] / "crates" / "amw-engine" / "build.rs"
_FIXTURE_VERIFIER = Path(__file__).parents[1] / "scripts" / "verify_engine_model_fixture.py"
_OFFICIAL_ATTESTATION = (
    Path(__file__).parent / "fixtures" / "attestations" / "github-docs-cli-attestation.sigstore.json"
)


class _AttestationResponse:
    def __init__(self, *, headers: dict[str, str], content: bytes = b"{}") -> None:
        self.headers = headers
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"attestations": [{"bundle": {}}]}


def _measured_materials() -> dict[str, str]:
    return {
        "source_tree": "b" * 40,
        "cargo_lock_sha256": "1" * 64,
        "workspace_manifest_sha256": "2" * 64,
        "engine_manifest_sha256": "3" * 64,
        "engine_build_sha256": "4" * 64,
        "vendor_tree_sha256": "5" * 64,
        "vendor_license_sha256": "6" * 64,
        "converter_requirements_sha256": "7" * 64,
        "native_fixture_model_url": binary_module._NATIVE_FIXTURE_MODEL_URL,
        "native_fixture_model_sha256": binary_module._NATIVE_FIXTURE_MODEL_SHA256,
        "native_fixture_license_url": binary_module._NATIVE_FIXTURE_LICENSE_URL,
        "native_fixture_license_sha256": binary_module._NATIVE_FIXTURE_LICENSE_SHA256,
    }


def _write_attestation_fixture(root: Path, *, digest: str) -> tuple[Path, Path]:
    artifact = root / "amw-engine-linux-cpu.zip"
    artifact.write_bytes(b"deterministic engine fixture")
    artifact_rows = [
        {
            "platform": platform,
            "accel": accel,
            "file": f"amw-engine-{platform}-{accel}.zip",
            "sha256": digest if (platform, accel) == ("linux", "cpu") else "1" * 64,
            "size_bytes": artifact.stat().st_size if (platform, accel) == ("linux", "cpu") else 1,
        }
        for platform in ("windows", "linux")
        for accel in ("cpu", "cuda")
    ]
    artifact_names = {row["file"] for row in artifact_rows}
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps({
            "engine_version": "0.1.0",
            "libllama_rev": "86a9c79f866799eb0e7e89c03578ccfbcc5d808e",
            "min_pkg_version": "0.6.0",
            "artifacts": artifact_rows,
            "provenance": {
                "repository": "StrategicMilk/AM-Workbench",
                "source_commit": "a" * 40,
                "source_ref": "refs/tags/v0.1.0",
                "workflow": "StrategicMilk/AM-Workbench/.github/workflows/engine.yml@refs/tags/v0.1.0",
                "run_id": "12345",
                "toolchain": {"rust": "1.88.0", "cuda": "12.4.1"},
                "deterministic_flags": {name: ["SOURCE_DATE_EPOCH=1722470400"] for name in artifact_names},
                "rebuild_inputs": {name: _measured_materials() for name in artifact_names},
            },
        }),
        encoding="utf-8",
    )
    return manifest, artifact


def test_mismatched_provenance_digest_fails_closed(tmp_path: Path) -> None:
    manifest, artifact = _write_attestation_fixture(tmp_path, digest="0" * 64)

    with pytest.raises(EngineBinaryCorruptError, match="SHA-256"):
        verify_release_manifest(manifest, artifact_path=artifact, installed_version="0.6.0")


def test_matching_provenance_digest_is_accepted(tmp_path: Path) -> None:
    expected = hashlib.sha256(b"deterministic engine fixture").hexdigest()
    manifest, artifact = _write_attestation_fixture(tmp_path, digest=expected)

    verified = verify_release_manifest(manifest, artifact_path=artifact, installed_version="0.6.0")

    assert verified["provenance"]["source_commit"] == "a" * 40


def test_official_github_docs_attestation_verifies_cryptographically() -> None:
    bundle = json.loads(_OFFICIAL_ATTESTATION.read_text(encoding="utf-8"))
    refresh_trust = os.environ.get("AMW_TEST_SIGSTORE_REFRESH") == "1"

    binary_module._verify_attestation_bundle(
        bundle,
        subject_name="gh_2.50.0_windows_arm64.zip",
        subject_sha256="8aad120b416386b4269ef62c8fdebcad31a70847297817a149daf927edc85548",
        repository="cli/cli",
        workflow_path=".github/workflows/deployment.yml",
        source_ref="refs/heads/trunk",
        source_commit="faef2ddd81b0736748413a7c646cd0bfc26c00a0",
        refresh_trust=refresh_trust,
    )


def test_tampered_official_attestation_and_wrong_identity_fail_closed() -> None:
    bundle = json.loads(_OFFICIAL_ATTESTATION.read_text(encoding="utf-8"))
    tampered = deepcopy(bundle)
    tampered["dsseEnvelope"]["payload"] = tampered["dsseEnvelope"]["payload"][:-2] + "AA"

    with pytest.raises(EngineBinaryCorruptError, match="cryptographic verification"):
        binary_module._verify_attestation_bundle(
            tampered,
            subject_name="gh_2.50.0_windows_arm64.zip",
            subject_sha256="8aad120b416386b4269ef62c8fdebcad31a70847297817a149daf927edc85548",
            repository="cli/cli",
            workflow_path=".github/workflows/deployment.yml",
            source_ref="refs/heads/trunk",
            source_commit="faef2ddd81b0736748413a7c646cd0bfc26c00a0",
            refresh_trust=False,
        )


def test_runtime_attestation_trust_is_refreshed_and_unavailability_is_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sigstore.verify import Verifier

    bundle = json.loads(_OFFICIAL_ATTESTATION.read_text(encoding="utf-8"))
    payload = base64.b64decode(bundle["dsseEnvelope"]["payload"], validate=True)
    offline_values: list[bool] = []

    class _Verifier:
        def verify_dsse(self, *_args: object) -> tuple[str, bytes]:
            return binary_module._ATTESTATION_PAYLOAD_TYPE, payload

    def _production(*, offline: bool) -> _Verifier:
        offline_values.append(offline)
        return _Verifier()

    monkeypatch.setattr(Verifier, "production", staticmethod(_production))
    binary_module._verify_attestation_bundle(
        bundle,
        subject_name="gh_2.50.0_windows_arm64.zip",
        subject_sha256="8aad120b416386b4269ef62c8fdebcad31a70847297817a149daf927edc85548",
        repository="cli/cli",
        workflow_path=".github/workflows/deployment.yml",
        source_ref="refs/heads/trunk",
        source_commit="faef2ddd81b0736748413a7c646cd0bfc26c00a0",
    )
    assert offline_values == [False]

    def _unavailable(*, offline: bool) -> _Verifier:
        raise OSError(f"trust root unavailable; offline={offline}")

    monkeypatch.setattr(Verifier, "production", staticmethod(_unavailable))
    with pytest.raises(EngineBinaryMissingError, match="trust material is unavailable"):
        binary_module._verify_attestation_bundle(
            bundle,
            subject_name="gh_2.50.0_windows_arm64.zip",
            subject_sha256="8aad120b416386b4269ef62c8fdebcad31a70847297817a149daf927edc85548",
            repository="cli/cli",
            workflow_path=".github/workflows/deployment.yml",
            source_ref="refs/heads/trunk",
            source_commit="faef2ddd81b0736748413a7c646cd0bfc26c00a0",
        )


def test_attestation_api_rejects_oversized_and_paginated_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _AttestationResponse(headers={"Content-Length": str(binary_module._MAX_ATTESTATION_RESPONSE_BYTES + 1)})
    monkeypatch.setattr(binary_module.httpx, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(EngineBinaryCorruptError, match="declared size budget"):
        binary_module._fetch_github_attestation_bundles("a" * 64, 1.0)

    response = _AttestationResponse(headers={"Link": '<https://api.github.com/next>; rel="next"'})
    monkeypatch.setattr(binary_module.httpx, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(EngineBinaryCorruptError, match="bounded page"):
        binary_module._fetch_github_attestation_bundles("a" * 64, 1.0)


def test_attestation_statement_requires_exact_subject_and_consistent_slsa_source() -> None:
    bundle = json.loads(_OFFICIAL_ATTESTATION.read_text(encoding="utf-8"))
    payload = base64.b64decode(bundle["dsseEnvelope"]["payload"], validate=True)

    with pytest.raises(EngineBinaryCorruptError, match="exactly one downloaded subject"):
        binary_module._verify_slsa_statement(
            payload,
            subject_name="nested/gh_2.50.0_windows_arm64.zip",
            subject_sha256="8aad120b416386b4269ef62c8fdebcad31a70847297817a149daf927edc85548",
            repository="cli/cli",
            workflow_path=".github/workflows/deployment.yml",
            source_ref="refs/heads/trunk",
            source_commit="faef2ddd81b0736748413a7c646cd0bfc26c00a0",
        )

    statement = json.loads(payload)
    statement["predicate"]["buildDefinition"]["resolvedDependencies"][0]["digest"] = {"gitCommit": "0" * 40}
    with pytest.raises(EngineBinaryCorruptError, match="expected source commit"):
        binary_module._verify_slsa_statement(
            json.dumps(statement).encode(),
            subject_name="gh_2.50.0_windows_arm64.zip",
            subject_sha256="8aad120b416386b4269ef62c8fdebcad31a70847297817a149daf927edc85548",
            repository="cli/cli",
            workflow_path=".github/workflows/deployment.yml",
            source_ref="refs/heads/trunk",
            source_commit="faef2ddd81b0736748413a7c646cd0bfc26c00a0",
        )

    wrong_predicate = json.loads(payload)
    wrong_predicate["predicateType"] = "https://example.invalid/predicate/v1"
    with pytest.raises(EngineBinaryCorruptError, match="unexpected statement type"):
        binary_module._verify_slsa_statement(
            json.dumps(wrong_predicate).encode(),
            subject_name="gh_2.50.0_windows_arm64.zip",
            subject_sha256="8aad120b416386b4269ef62c8fdebcad31a70847297817a149daf927edc85548",
            repository="cli/cli",
            workflow_path=".github/workflows/deployment.yml",
            source_ref="refs/heads/trunk",
            source_commit="faef2ddd81b0736748413a7c646cd0bfc26c00a0",
        )

    with pytest.raises(EngineBinaryCorruptError, match="cryptographic verification"):
        binary_module._verify_attestation_bundle(
            bundle,
            subject_name="gh_2.50.0_windows_arm64.zip",
            subject_sha256="8aad120b416386b4269ef62c8fdebcad31a70847297817a149daf927edc85548",
            repository="attacker/repository",
            workflow_path=".github/workflows/deployment.yml",
            source_ref="refs/heads/trunk",
            source_commit="faef2ddd81b0736748413a7c646cd0bfc26c00a0",
            refresh_trust=False,
        )


def test_engine_workflow_uses_runner_native_cmake_flags() -> None:
    workflow = yaml.load(_ENGINE_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    rows = workflow["jobs"]["build"]["strategy"]["matrix"]["include"]
    by_bundle = {row["bundle"]: row for row in rows}

    for bundle in ("windows-cpu", "windows-cuda"):
        row = by_bundle[bundle]
        assert row["cflags"] == "/O2"
        assert row["cxxflags"] == "/O2"
        assert row["cmake_linker_flags"] == "/Brepro"

    for bundle in ("linux-cpu", "linux-cuda"):
        row = by_bundle[bundle]
        assert "-ffile-prefix-map=" in row["cflags"]
        assert "-fdebug-prefix-map=" in row["cxxflags"]
        assert row["cmake_linker_flags"] == "-Wl,--build-id=none -static-libgcc -static-libstdc++"


def test_engine_workflow_enforces_exact_bundle_and_scoped_supply_chain() -> None:
    workflow_text = _ENGINE_WORKFLOW.read_text(encoding="utf-8")

    assert "cargo deny check licenses bans sources --config deny.toml" in workflow_text
    assert "cargo install cargo-audit --version 0.22.2 --locked" in workflow_text
    assert "scripts/check_rustsec_package.py --package amw-engine" in workflow_text
    assert 'not p.name.startswith(".")' in workflow_text
    assert "unexpected = names - required" in workflow_text
    assert "if missing or unexpected:" in workflow_text
    assert "AZURE_CLIENT_SECRET" not in workflow_text
    assert "--clobber" not in workflow_text
    assert "environment: engine-release-signing" in workflow_text
    assert "Azure/login@93381592711f247e165c389ebb30b596c84cdc48" in workflow_text
    assert "actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26" in workflow_text
    assert "path: build-handoff" in workflow_text
    assert "path: signed-handoff" in workflow_text
    assert "path: publish-handoff" in workflow_text
    assert "Assert flat downloaded handoffs" in workflow_text
    assert "Assert flat publisher input" in workflow_text
    assert "LICENSE.llama.cpp" in workflow_text
    assert "ENGINE_THIRD_PARTY_LICENSES.md" in workflow_text
    assert "--require-hashes" in workflow_text
    assert "scripts/check_converter_lock.py" in workflow_text
    assert "scripts/verify_llama_vendor.py" in workflow_text
    assert 'vendor / "conversion"' in workflow_text
    assert 'vendor / "gguf-py" / "gguf"' in workflow_text
    assert "Validate and smoke-test the packaged archive" in workflow_text
    assert "scripts/validate_engine_release.py archive" in workflow_text
    assert "amw-engine-server" in workflow_text
    assert "cache-dependencies: false" in workflow_text
    assert "exclude-shared-token-cache-credential: true" in workflow_text
    assert "ENGINE_WINDOWS_SIGNER_SUBJECT" in workflow_text
    assert "ENGINE_WINDOWS_SIGNER_ISSUER" in workflow_text
    assert workflow_text.count("sbom-path: release-sboms/amw-engine-") == 4
    assert "--predicate-type https://spdx.dev/Document/v2.3" in workflow_text
    assert "BUILD_SHARED_LIBS=OFF" in workflow_text
    assert "-static-libgcc -static-libstdc++" in workflow_text
    assert "ENGINE_RELEASE_ADMIN_TOKEN" in workflow_text
    assert workflow_text.count("repos/$REPOSITORY/immutable-releases") == 2
    assert workflow_text.count("git ls-remote --exit-code") == 2
    assert "source_tree" in workflow_text
    assert "vendor_tree_sha256" in workflow_text
    publisher = workflow_text.split("\n  publish:\n", maxsplit=1)[1].split("\n  verify-release:\n", maxsplit=1)[0]
    assert "actions/checkout" not in publisher
    assert "persist-credentials" not in publisher


def test_native_fixture_is_commit_pinned_bounded_and_license_governed() -> None:
    workflow_text = _ENGINE_WORKFLOW.read_text(encoding="utf-8")
    reference_text = _ENGINE_REFERENCE.read_text(encoding="utf-8")
    verifier_text = _FIXTURE_VERIFIER.read_text(encoding="utf-8")

    assert (
        "https://huggingface.co/tensorblock/tinyllama-15M-stories-GGUF/resolve/"
        "51b755181aac158c3ee689c0bd86f49a8291d1da/tinyllama-15M-stories-Q2_K.gguf"
    ) in workflow_text
    assert "f7e39dc9f26f3d39bf59e885349c6eec65880f685322d591f53e6cdb46ceb2e9" in workflow_text
    assert "MODEL_MAX_BYTES = 16_777_216" in verifier_text
    assert (
        "AMW_ENGINE_NATIVE_TEST_MODEL: ${{ runner.temp }}/amw-engine-native-fixture/tinyllama-15M-stories-Q2_K.gguf"
    ) in workflow_text
    assert 'cargo test -p amw-engine --features "${{ matrix.test_feature }}" --locked' in workflow_text
    assert "actions/cache" not in workflow_text
    assert "scripts/verify_engine_model_fixture.py" in workflow_text
    assert "MIT-licensed" in reference_text
    assert "13,717,344 bytes" in reference_text
    assert "c8434895da38a8720e24712d2d79a0b4dfba77c94a5307ac974f44c194ad0af7" in workflow_text
    assert "engine-native-fixture-license-evidence" in workflow_text
    assert "c8434895da38a8720e24712d2d79a0b4dfba77c94a5307ac974f44c194ad0af7" in reference_text


def test_cuda_build_links_ggml_backend_and_required_toolkit_libraries() -> None:
    build_script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'println!("cargo:rustc-link-lib=static=ggml-cuda")' in build_script
    for library in ("cublas", "cublasLt", "cudart", "cuda"):
        assert library in build_script
    assert 'toolkit.join("lib64/stubs")' in build_script
    assert 'toolkit.join("lib/x64")' in build_script


def test_runtime_provisioner_uses_sigstore_api_without_gh_cli() -> None:
    facade_source = Path(binary_module.__file__).read_text(encoding="utf-8")
    attestation_source = Path(attestation_module.__file__).read_text(encoding="utf-8")
    workflow = _ENGINE_WORKFLOW.read_text(encoding="utf-8")
    reference = _ENGINE_REFERENCE.read_text(encoding="utf-8")

    assert "_verify_github_attestation" in facade_source
    assert "Verifier.production(offline=not refresh_trust)" in attestation_source
    assert "OIDCBuildSignerURI" in attestation_source
    assert "OIDCSourceRepositoryDigest" in attestation_source
    assert ".github/workflows/engine.yml" in attestation_source
    assert 'subprocess.run(["gh"' not in facade_source + attestation_source
    assert "create-storage-record:" not in workflow
    assert "artifact-metadata: write" not in workflow
    assert "storage records only for a single OCI subject" in reference
