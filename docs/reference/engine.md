# AM Engine operations, build, and distribution

## Operator model lifecycle

### Install and choose a runtime

AM Engine uses one versioned executable under the per-user Vetinari directory:
`<user-dir>/engine/v0.1.0/amw-engine-server[.exe]`. Set
`VETINARI_USER_DIR` to relocate the whole per-user tree, or set
`VETINARI_ENGINE_BINARY_PATH` to an existing development binary. Every selected
binary must pass the pinned `--version` probe before the supervisor starts it.

The first-party provisioner currently fails closed before network access because
the independently verified public release commit pin is intentionally unset.
There is no supported public download until an immutable release exists and a
subsequent package update records its peeled commit. Do not fill the pin from a
manifest's self-declared commit or bypass the attestation checks. Repository
developers can build an explicit local binary instead:

```text
cargo build -p amw-engine --release --features cpu --locked
cargo build -p amw-engine --release --features cuda --locked
```

Choose exactly one feature for a release build:

| Bundle | Use when | Host prerequisites |
| --- | --- | --- |
| CPU | No compatible NVIDIA CUDA runtime is available, or portability matters more than throughput. | Supported Windows/Linux host and sufficient RAM. |
| CUDA | The host has a compatible NVIDIA GPU and the model fits the configured VRAM/RAM budgets. | CUDA 12.4 runtime, compatible NVIDIA driver, and the external `cublas`, `cublasLt`, `cudart`, and `cuda` libraries. |

A CUDA binary does not silently fall back to the CPU bundle when its dynamic
runtime is missing. Install the prerequisites or select the CPU build, then run
the version and readiness checks again.

### Place and discover GGUF models

The owned supervisor generates `<user-dir>/engine/runtime/engine.toml`. Model
discovery reads the directories in `[models].dirs`; the
`VETINARI_ENGINE_MODELS_DIRS` environment override accepts a semicolon-separated
list. A direct server invocation may repeat `--model-dir`. Put each `.gguf` file
under one of those roots before starting the engine.

Discovery runs at process startup. It is symlink-safe, traverses at most eight
directory levels, and admits at most 10,000 GGUF files across all roots. A
missing, unreadable, or symlinked root and an exceeded aggregate model limit
fail engine initialization. A corrupt model or invalid sidecar is isolated so a
valid sibling can still be admitted. Changing model roots or adding a file
requires an engine restart; `/admin/config/reload` does not rescan models.

### List, load, and verify models

An admin-scoped credential can list the startup catalog with:

```text
GET /admin/models/catalog?schema_version=1&limit=100&offset=0&rejected_limit=100&rejected_offset=0
```

`limit` and `rejected_limit` default to 100 and must be between 1 and 256.
`offset` and `rejected_offset` page admitted and rejected rows independently.
`model_count`, `rejected_count`, `next_model_offset`, and
`next_rejected_offset` let a caller finish either page set without requesting an
unbounded response. Admitted rows expose the stable model ID, bounded GGUF
metadata, and capability flags. Rejected rows expose only a candidate basename,
fixed reason code, and operator-safe reason; configured roots, absolute paths,
and raw parser diagnostics are never returned.

```json
{
  "schema_version": 1,
  "models": [
    {
      "id": "qwen-local",
      "architecture": "qwen2",
      "quantization": "Q4_K",
      "context_length": 32768,
      "embedding_length": 3584,
      "supports_embeddings": false,
      "supports_fim": false
    }
  ],
  "rejected": [
    {
      "candidate_name": "damaged.gguf",
      "reason_code": "integrity",
      "reason": "GGUF metadata or tensor bounds failed validation"
    }
  ],
  "model_count": 1,
  "rejected_count": 1,
  "next_model_offset": null,
  "next_rejected_offset": null
}
```

Discovery does not allocate model weights. Load a returned stable ID through
`POST /admin/models/load` with
`{"schema_version":1,"model_id":"qwen-local"}`. Confirm it through
`GET /admin/models/status?schema_version=1&model_id=qwen-local`, then check the
authenticated `/readyz` response. `control_ready=true` means the control plane
is running; `data_ready=true` and `ready=true` require at least one loaded model
and a non-draining runtime.

### Logs, crashes, restart, and rollback

The default owned-runtime log directory is
`<user-dir>/engine/runtime/logs`. `engine.jsonl` rotates at 16 MiB with three
backups, and each structured record is capped at 256 KiB. An unrecoverable Rust
panic writes an atomic, content-redacted `engine-crash.json` capped below 64 KiB
in the same directory. Preserve both files before restarting after a persistent
failure. Supervisor status retains only a bounded startup tail; credentials and
absolute host paths are redacted before any line enters that operator-facing
snapshot.

The supervisor restarts an unexpectedly exited owned child with capped
exponential backoff: five attempts by default, starting at 0.5 seconds. A child
that remains healthy for 60 seconds resets the attempt counter. After the budget
is exhausted, the supervisor reports `degraded`; inspect the logs and catalog,
fix the underlying binary/config/model issue, and call the owned supervisor's
`restart()` operation. For a live owned child it performs drain → bounded stop →
verified start; after automatic recovery is exhausted, it resets that budget and
starts a new owned generation from the stopped state. Both paths require a
strictly newer endpoint generation. It refuses adopted/shared processes and
ordinary stopped states that did not exhaust automatic recovery. Live clients
use `drain()` and `undrain()` only to toggle admission;
`/admin/config/reload` is intentionally not exposed as a success-shaped no-op.
Do not delete crash reports or authentication files to force a restart.

For a model regression, unload the new stable ID with
`POST /admin/models/unload`, load the previously verified ID, and recheck
`/readyz`. Binary activation automatically restores the previous installation
only when activation itself fails. There is no one-click post-activation binary
rollback: preserve a verified prior bundle before upgrade and follow
[Upgrade, Migration, and Rollback](../runbooks/upgrade-migration-rollback.md).
The restored executable must still match the package's expected engine version.

For support, collect `vetinari doctor --json`, `vetinari status`, the bounded
catalog response, and the relevant `engine.jsonl`/`engine-crash.json` records.
Review and redact local usernames, model names, absolute paths, tokens, prompt
content, and source excerpts before sharing. Never include `auth.token`,
`auth-policy.json`, model weights, registry sidecars, session data, or the full
per-user engine directory.

### Provision the Linux protected receipt signer

Linux production receipt signing is an external service-provisioning boundary,
not an in-process key-generation feature. This repository defines and verifies
the contract below; it does not claim that a host, token, HSM, PIN, or protected
service account has been provisioned. Run the engine as a dedicated non-root
account. If its effective UID is `1001`, the signed receipt trust anchor must
contain the exact service identity `uid:1001`; root and a UID that differs from
the anchor are rejected.

For an absolute trust-anchor path `<anchor>`, provision the provider record at
the exact sibling path `<anchor>.pkcs11.toml`. For example,
`/var/lib/vetinari-engine/receipt-anchor.json` requires
`/var/lib/vetinari-engine/receipt-anchor.json.pkcs11.toml`. The record has no
optional or extension fields:

```toml
schema_version = 1
installation_id = "<exact installation_id from the signed anchor>"
key_id = "<lowercase SHA-256 of the anchor public_key_spki_der bytes>"
module_path = "/usr/lib/vendor-pkcs11.so"
module_sha256 = "<lowercase SHA-256 of the module bytes>"
token_label = "<exact token label>"
token_serial = "<exact token serial>"
key_object_id = "<1-to-128-byte CKA_ID encoded as lowercase hex>"
key_label = "<exact CKA_LABEL>"
user_pin_path = "/var/lib/vetinari-engine/secrets/pkcs11-user.pin"
```

`module_path` and `user_pin_path` must be absolute normalized paths. The module
must be a non-empty, root-owned regular file beneath non-writable root-owned,
non-symlink directories; group/other write bits are forbidden and its bytes
must match `module_sha256`. The TOML record and PIN must each be non-empty,
single-link regular files owned by the engine effective UID with exact mode
`0600`, opened without following symlinks. Each immediate parent must be owned
by that UID with exact mode `0700`; all higher ancestors must be root- or
service-owned, non-writable, real directories. The PIN is UTF-8, at most 4 KiB,
and may have one terminal newline, but may not otherwise contain NUL, CR, or LF.
Do not pass the module, token, key, config, or PIN through an environment
variable or request parameter.

The token label plus serial must select exactly one live token. The private and
public key objects must have the same configured `CKA_ID` and `CKA_LABEL`. The
private object must be a token-resident EC private key with
`CKA_PRIVATE=true`, `CKA_SENSITIVE=true`, `CKA_ALWAYS_SENSITIVE=true`,
`CKA_EXTRACTABLE=false`, `CKA_NEVER_EXTRACTABLE=true`, and `CKA_SIGN=true`.
The public object must be a token-resident, non-private EC public key with
`CKA_VERIFY=true`. Both objects must declare the P-256 EC parameters; the public
`CKA_EC_POINT` must reconstruct the canonical P-256 SPKI stored in the signed
anchor, and the token must advertise signing-capable `CKM_ECDSA`.

At startup the engine re-hashes the module, logs into the exact token, attests
both objects, and asks the non-exportable key to sign an anchor-bound
proof-of-possession challenge covering the anchor digest, public-key digest,
and installation ID. The same PKCS#11 path serves anchors whose provider is
`pkcs11` or `hsm`; the provider label alone does not waive any check. There is
no native Linux `tpm` resolver: a TPM-backed key is supported only when a pinned
PKCS#11 module exposes it through this exact path and the anchor uses `pkcs11`
or `hsm`; a `tpm` provider value fails closed. Partial
receipt configuration, an unavailable module/token/PIN, selector ambiguity,
attribute drift, SPKI mismatch, failed proof of possession, ledger identity or
lineage failure, or a mutable protected path aborts engine startup. After
startup, ledger integrity failure makes `/readyz` return service unavailable
with `receipt_ledger_unready`; there is no software-key or file-key fallback.

Provision and verify a host in this order:

1. While the service is stopped, create the token P-256 key pair with the exact
   attributes above, record the provider-reported label and serial, and derive
   the anchor SPKI and `key_id` from the public object. Independently sign the
   trust anchor with the pinned receipt-authority key.
2. Install the root-owned module, service-owned PIN, provider TOML, signed
   anchor, and protected ledger path. Record `id -u`, `stat` owner/mode/type/link
   facts, the module SHA-256, token identity, object attributes, public SPKI,
   anchor digest, and authority-pin digest in the deployment evidence.
3. Start the engine as that non-root UID. Require `/version` to report a
   `receipt_trust` record whose installation ID, provider, service identity,
   key ID, key epoch, anchor digest, engine binary, release manifest, source
   commit, and release revisions equal the provisioned authorities.
4. Load the governed model and require `/readyz` to return `ready=true` without
   `receipt_ledger_unready`. Submit one authenticated evaluation request with a
   fresh `run_id`, `suite_id`, `case_id`, ordinal, and exact suite/case digests;
   retain the terminal signed receipt and verify its P-256 signature, anchor
   identity, epoch, and immutable ledger row. A healthy HTTP process without
   this live receipt round trip is not provisioning evidence.

For rotation, stop the service, create and attest a new token key pair, and
issue a newly authority-signed anchor with a strictly greater `key_epoch`. Its
complete predecessor epoch, key ID, and anchor SHA-256 must equal the latest
ledger entry. Stage the new anchor, sibling TOML, PIN reference, and configured
anchor digest while stopped; keep the old key available until the new startup,
`/version`, `/readyz`, and live receipt checks pass. If startup fails before the
new epoch registers, restore the exact prior files and key. Once the ledger has
registered the new epoch, rollback to the old anchor is intentionally rejected;
recover forward with another higher epoch linked to the registered predecessor.
Loss of the module, token, PIN, private key, authority signature, or ledger is a
hard outage: restore the exact protected provider state from an operator-held
backup or perform an authorized forward rotation, never delete/reset the ledger
or substitute a software key.

## Runner requirements

The release matrix builds Windows CPU, Windows CUDA, Linux CPU, and Linux CUDA bundles. Every leg uses Rust 1.88.0 and CMake in Release mode. CUDA legs install CUDA Toolkit 12.4.1 through the commit-pinned `Jimver/cuda-toolkit` action and prove only that `nvcc`, CMake, Rust, and the CUDA-linked build path are available. A standard hosted runner has no qualifying GPU and therefore does not certify CUDA runtime behavior or model offload. Publishing adds a separate protected `cuda-certification` job in the `vetinari-engine-gpu` runner group. That runner must provide the root-owned `/opt/vetinari/bin/amw-engine-cuda-certifier`; the protected environment pins its SHA-256 as `ENGINE_CUDA_CERTIFIER_SHA256`. The verifier consumes the exact same-run Linux CUDA artifact and pinned GGUF fixture, then emits device identity and a positive device-memory delta for actual model offload. Its JSON is uploaded as `engine-cuda-certification`; the publication gate downloads that artifact and rejects a source commit, immutable run URL, governed runner identity, or fixture digest that does not match the current run. Editing the committed blocked example cannot unlock release. CPU legs require only the hosted runner's CMake and pinned Rust toolchain.

The Rust server is built with `cargo build -p amw-engine --release --features cpu|cuda --locked`. CUDA builds statically include the vendored `ggml-cuda` archive under llama.cpp's MIT license, but dynamically link the external CUDA 12.4.1 `cublas`, `cublasLt`, `cudart`, and `cuda` libraries. Those NVIDIA libraries are runtime prerequisites and are not copied into the AM Engine archive; a matching CUDA 12.4 runtime and NVIDIA driver must therefore be installed on the host. The bundle retains the hash-pinned CUDA EULA and its cuBLAS third-party notices as prerequisite license evidence without claiming that NVIDIA binaries are redistributed. The vendored imatrix generator and quantizer are built from `crates/amw-engine/vendor/llama.cpp` with `BUILD_SHARED_LIBS=OFF`, `GGML_OPENMP=OFF`, `LLAMA_CURL=OFF`, and `GGML_CUDA=ON|OFF`, followed by `cmake --build ... --target llama-quantize llama-imatrix`. `GGML_STATIC` remains disabled, so CUDA toolkit libraries are still dynamically linked even though the project libraries are static. Linux native tools statically link the C and C++ compiler runtimes. Every finished archive is safely extracted into a clean temporary directory and `amw-engine-server --version`, `llama-imatrix --help`, `llama-quantize --help`, and both converters' `--help` paths must execute before the build artifact is accepted. The converter interpreter and every native tool are resolved to absolute paths before a helper changes its working directory.

## Governed native-test model

Every Windows/Linux CPU/CUDA build leg runs the native Rust integration tests against tensorblock's MIT-licensed `tinyllama-15M-stories-GGUF` fixture. The only accepted source is [`tinyllama-15M-stories-Q2_K.gguf` at Hugging Face commit `51b755181aac158c3ee689c0bd86f49a8291d1da`](https://huggingface.co/tensorblock/tinyllama-15M-stories-GGUF/resolve/51b755181aac158c3ee689c0bd86f49a8291d1da/tinyllama-15M-stories-Q2_K.gguf); its SHA-256 is `f7e39dc9f26f3d39bf59e885349c6eec65880f685322d591f53e6cdb46ceb2e9` and its expected size is 13,717,344 bytes. The [commit-pinned repository card](https://huggingface.co/tensorblock/tinyllama-15M-stories-GGUF/resolve/51b755181aac158c3ee689c0bd86f49a8291d1da/README.md) declares MIT and has SHA-256 `c8434895da38a8720e24712d2d79a0b4dfba77c94a5307ac974f44c194ad0af7`. CI independently downloads, bounds, hashes, validates, and retains that card as the `engine-native-fixture-license-evidence` artifact. The source and license identities also appear in every measured rebuild-material record.

The workflow downloads at most 16,777,216 bytes into the runner-temporary `AMW_ENGINE_NATIVE_TEST_MODEL` path, validates any declared content length, rejects empty/oversized content, verifies the exact digest and size, and only then runs `cargo test -p amw-engine --features cpu|cuda --locked`. The fixture is never copied into a release bundle and is not placed in an Actions cache, so each clean leg re-verifies source bytes without creating an unbounded or mutable model cache.

## Signing model

Windows release binaries use Authenticode through Azure Trusted Signing, the pinned default certificate authority for this workflow. Changing certificate authorities is an OPERATOR decision: it requires an account controlled by the owner and a corresponding decision record in the ENG-P09 CI evidence. An executor must not silently substitute a CA.

Release signing runs in the protected `engine-release-signing` GitHub environment after compilation. The signing jobs download only immutable Actions artifacts; they never check out or execute repository source. `Azure/login` exchanges the job's short-lived GitHub OIDC identity for Azure access, so the workflow stores no Azure client secret. Environment variables provide the tenant, client, subscription, signing endpoint, signing account, and certificate profile identifiers. The Azure federated credential must be restricted to the repository and `engine-release-signing` environment, and that environment must require the owner's release approval.

A release run fails if `ENGINE_SIGNING_ENABLED` is not `true`, an OIDC identifier is absent, token exchange fails, signing fails, any of the three native executables lacks a valid Authenticode signature, or the signature cannot be read. The three signed members are `amw-engine-server.exe`, `llama-imatrix.exe`, and `llama-quantize.exe`; no extra executable is accepted. The protected environment must also define the exact expected certificate subject and issuer; each signed executable must match both and assert the code-signing EKU. The signing action disables its dependency cache and excludes shared-token-cache authentication. Because Authenticode changes executable bytes, each signing job downloads the same-run committed size ledger and validator, then enforces the Windows CPU/CUDA ceiling against the signed `amw-engine-server.exe` before rebuilding its inner manifest, archive digest, and release fragment. Signed Windows archives are subsequently downloaded into an unprivileged clean validation job and passed through the same archive and executable smoke helper before assembly. Unsigned development runs require the explicit `workflow_dispatch` `dev_mode` choice and cannot reach publication. Signing steps do not enable shell tracing or print access tokens. Each release line also requires the operator to retain the provider's malware-analysis submission/result with the hosted CI record.

## Immutable release procedure

Enable immutable releases for the public `StrategicMilk/AM-Workbench` repository before the first engine release. Store an admin-read token as `ENGINE_RELEASE_ADMIN_TOKEN` in the protected `engine-release-publish` environment; it is used only to fail closed against GitHub's immutable-release settings endpoint. The environment must require owner approval and define `ENGINE_RELEASE_EXCLUSIVE_WRITER_ENABLED=true`, the exact dispatch identity in `ENGINE_RELEASE_EXPECTED_ACTOR`, and the exact GitHub asset-uploader login in `ENGINE_RELEASE_EXPECTED_UPLOADER`. Create `v{engine_version}` as a draft release with no assets, then dispatch this workflow from that exact tag with `publish_release=true`, `measure_only=false`, and `dev_mode=false`. The workflow validates that the engine version and tag agree, builds and signs the assets, and publishes the draft only after all provenance checks pass.

The `contents: write` publisher has no source checkout and receives only the flat seven-file `attested-engine-release` Actions artifact plus the exact validator copied from the source run. That validator is deliberately standard-library-only and carries its closed release vocabulary internally, so publication does not trust undeclared runner-global packages or an installed Vetinari checkout; clean-environment tests keep the vocabulary in parity with the runtime contract. Its non-cancelling concurrency group spans the critical window. Before upload, and again immediately before publication, it requires the repository immutable-release setting to be enabled and resolves the remote Git tag to its peeled commit, which must equal `github.sha`. It resolves the empty draft once, records that numeric release ID, and uploads each asset directly to that ID. The binding records every returned asset ID plus the expected name, byte size, SHA-256 digest, uploader, tag, repository, and source commit. Immediately before publication, the validator fetches the draft by numeric ID and requires the exact binding and local handoff. Publication uses `PATCH /repos/{owner}/{repo}/releases/{release_id}` with `draft=false`; it never mutates a release selected by tag. It then immediately fetches both the same numeric ID and the tag mapping, requiring both to identify the bound release with the exact assets, `draft=false`, and `immutable=true`.

GitHub's release mutation API does not provide a compare-and-swap precondition for this transition. The concurrency group excludes cooperating workflow runs only; it cannot exclude an unrelated credential or workflow that ignores the group. Safe publication therefore retains one explicit residual trust assumption: during the bound critical window, the protected environment and repository credential policy must make this job the exclusive `contents: write` actor for the draft. The protected identity variables are an operator attestation of that repository configuration, not proof supplied by GitHub. If exclusivity or identity configuration is absent, the workflow fails closed. A filename collision, tag movement, release-ID change, uploader change, asset-ID change, settings change, partial draft, remote byte drift, or failed same-ID/tag immutability recheck blocks completion. Recovery is to delete the unpublished draft and create a clean draft rather than overwrite an asset. Once published, the separate verification job checks GitHub's immutable-release attestation, re-downloads every asset, verifies every GitHub-computed asset digest and artifact-specific SPDX attestation, and repeats executable archive smoke on the runnable CPU assets. CUDA publication cannot reach this point without the governed GPU/offload certification prerequisite.

## Binary size budget

Run `workflow_dispatch` once with `measure_only=true` to produce a green, non-publishing measurement for all four legs. Record each server binary's byte size and immutable workflow URL in `docs/reference/engine-size-baselines.json`. For each leg, compute the ceiling as `ceil((baseline_bytes * 1.20) / 1 MiB) * 1 MiB`; the 20 percent margin absorbs toolchain noise without hiding sustained growth.

Repository variables are not a size authority. Non-publishing builds report actual and proposed values, while release publication fails when any committed baseline, ceiling, or run URL is absent or when the server exceeds the committed ceiling. The ledger is deliberately unmeasured in this checkout, so publication is currently blocked. Raising a ceiling requires a new measured hosted baseline and evidence entry; an arbitrary or undocumented value is invalid.

## Bundle layout

Each deterministic `amw-engine-{platform}-{accel}.zip` contains exactly the required distribution inputs:

- `amw-engine-server` or `amw-engine-server.exe`
- `llama-imatrix` or `llama-imatrix.exe`
- `llama-quantize` or `llama-quantize.exe`
- `convert_hf_to_gguf.py`
- `convert_lora_to_gguf.py`
- the complete `conversion/` and `gguf-py/gguf/` Python package trees used by the converter
- a self-contained, transitive `requirements-convert_lora_to_gguf.txt` with exact pins and SHA-256 hashes for every wheel
- `LICENSE.llama.cpp`, byte-for-byte equal to the full upstream llama.cpp MIT license
- `NOTICE`, copied from `crates/amw-engine/NOTICE`
- `ENGINE_THIRD_PARTY_LICENSES.md`, generated from the exact reachable Rust graph and governed two-platform converter-lock union
- `ENGINE_LICENSES/`, containing exact package metadata and package-provided or revision-pinned supplemental license/copyright files for every reachable Rust crate and every platform-active hash-locked converter wheel; each archive includes a platform/accelerator-specific `SPDX.spdx.json`, and CUDA archives also record the four external dynamic NVIDIA prerequisites and retain the official pinned terms/notices document
- the in-bundle `manifest.json`

The converter environment installs only binary wheels with `--require-hashes`; those exact installed wheels supply the converter license texts staged into the corpus. Cargo package metadata supplies the reachable Rust package authors, repositories, and package-provided license files. When a published crate or wheel omits its upstream license file, staging accepts only a package/version-specific supplemental notice checked into the repository from an immutable upstream revision with its expected SHA-256; it never borrows another package's copyright notice. The corpus index carries canonical sorted `name==version` closure digests for the 221 reachable Cargo packages, 31 platform-active Windows converter packages, and 30 platform-active Linux converter packages, and the provisioner recomputes those digests and binds each metadata file to its index row. A missing hash, source distribution, unexpected index, stale or fabricated identity, metadata disagreement, unresolved fresh platform resolution, or absent license text blocks release. Every ZIP member is a regular file with exact mode `0755` for Linux native executables and `0644` otherwise. The in-bundle manifest hashes every inner file except itself. It never hashes its containing zip. The standalone release `manifest.json` is the authority for bundle hashes, resolving the circular self-hash prohibited by R10. Each build, signing, assembly, and publishing transition stages one exact flat directory before `upload-artifact`; consumers reject nested paths so `upload-artifact`'s multi-path common-ancestor behavior cannot silently change the handoff contract.

## Manifest and provenance

The IS3.7 fields are `engine_version`, `libllama_rev`, `min_pkg_version`, and `artifacts`. Each artifact row contains exactly `platform`, `accel`, `file`, `sha256`, and `size_bytes`. Engine version comes from `crates/amw-engine/Cargo.toml`, and a release tag must equal `v{engine_version}`. The llama.cpp revision comes from `[package.metadata.am_engine].libllama_rev`; the minimum package version comes from `vetinari.__version__`, the dynamic `pyproject.toml` project version authority.

The additive R70/G10 `provenance` record binds the manifest to the public repository, exact tag ref, source commit, workflow identity, workflow run ID, and pinned Rust/CUDA toolchains. `deterministic_flags` and `rebuild_inputs` are keyed by all four artifact filenames; missing, extra, duplicate, or malformed per-leg values fail assembly and runtime verification. Rebuild inputs are measured rather than declared: they contain the checked-out Git tree, hashes for the Cargo lock and manifests, `build.rs`, the complete vendor tree, the upstream license, the converter requirements, and the governed model/license URLs and hashes. The supply-chain job independently downloads llama.cpp commit `86a9c79f866799eb0e7e89c03578ccfbcc5d808e`, normalizes CRLF line endings, sorts paths using repository-relative POSIX names, and requires both that upstream tree and the checked-in vendor tree to equal SHA-256 `fec1a92e3956bb2eec6f6f4b7f5a5b4111fe3461accd4cb58efb0068eca6c3ab`; the Cargo metadata revision must agree. All four legs must report the identical material record. Builds pin `SOURCE_DATE_EPOCH`, remap source paths, request deterministic linker/archive behavior, and normalize zip timestamps and ordering. The Linux CPU leg currently proves component-level server reproducibility with a clean rebuild and SHA-256 comparison; the workflow does not claim full archive or quantizer reproducibility until a fresh hosted checkout rebuild compares those final and component digests.

The unprivileged assembly job runs the executable archive/merge validator before creating the standalone manifest. It extracts each archive's artifact-specific SPDX document and uses GitHub OIDC and the pinned `actions/attest` action to issue both Sigstore build-provenance attestations and one SPDX 2.3 SBOM attestation per bundle. The action's Artifact Metadata storage-record path is intentionally not enabled: the pinned action supports storage records only for a single OCI subject with `push-to-registry`, not this multi-file GitHub Release. Publishing the draft creates GitHub's separate immutable-release attestation. Missing or unverifiable identity/provenance, an SPDX selector mismatch, a re-downloaded bundle hash mismatch, a minimum-package incompatibility, or an unsafe manifest row fails closed.

## First-party provisioning

`vetinari.engine.binary.provision_binary()` no longer downloads llama.cpp's upstream `b10066` archives. It reads the pinned first-party `StrategicMilk/AM-Workbench` release through GitHub's Releases API and requires the exact tag to be published and immutable. GitHub's immutable asset name, size, digest, and first-party download URL must agree with the standalone manifest before a bundle is downloaded. Assembly emits `consumer-release-authority.json`, binding the peeled source commit, the standalone manifest digest, and the exact inner-manifest digest for every platform/accelerator bundle. That authority file is a separately attested immutable release asset. A subsequent consumer update independently verifies it, then records all three authorities in package source; they are intentionally unset before the first hosted immutable release exists. Provisioning therefore fails before network access until real post-publication authority is retained. A manifest's self-declared source commit or a writable installed manifest never becomes the trust root.

The provisioner selects exactly one Windows/Linux x CPU/CUDA artifact for the local platform and first verifies the downloaded standalone manifest against the package-retained authority digest. Before extraction or execution, it downloads one bounded page of the subject's GitHub artifact-attestation bundles and uses the pinned `sigstore` Python verifier with an online production-root refresh. There is no unverified offline fallback or stale cache acceptance path. The certificate policy binds the public repository, `.github/workflows/engine.yml`, immutable tag ref, independently pinned source commit, GitHub-hosted runner, and build-config identity; the signed DSSE statement must bind the exact selected filename and digest plus the same SLSA workflow and resolved source commit. The runtime does not require or invoke `gh`. It then rejects unsafe archive members, validates the exact licensed converter/tool tree and every inner-manifest digest, requires the selected inner manifest to equal its package-retained digest, probes `amw-engine-server --version`, and atomically activates the complete tree. Missing network data, incomplete authority pins, unavailable trust material, a fabricated digest-consistent archive/manifest without a valid attestation, a mutable release, wrong repository/ref/workflow identity, missing matrix leg, malformed measured provenance, upstream URL, content mismatch, or failed probe leaves the prior installation in place.

Consumers import `vetinari.engine.binary_bundle.resolve_bundle_tool` and pass one of the closed logical names `convert_hf_to_gguf`, `convert_lora_to_gguf`, `imatrix`, or `quantize`. Resolution first requires the complete package-retained authority and matches the installed inner-manifest bytes to exactly one retained bundle digest. It then revalidates the canonical installed tree, license corpus, artifact digests, platform/accelerator selectors, and Unix executable modes before returning an absolute path. A coherent executable-plus-manifest rewrite therefore fails before the mutable manifest can authorize its own replacement bytes. Unknown names, missing or altered members, symlinks, escaped paths, incomplete licenses, and unexpected files also fail closed. The resolver never searches `PATH`, the checkout, the vendored llama.cpp tree, or environment overrides.

Run `.venv312/Scripts/python.exe scripts/probe_engine_export_toolchain.py --converter-python <locked-converter-python> --receipt <path>` against a freshly provisioned host bundle to prove the post-release consumer seam. That mode is unavailable until the consumer authority update is present. To avoid circularly requiring future authority before the first authentic release exists, the signed Windows release jobs instead re-download and validate the signed archive, revalidate exact signer subject/issuer/EKU, retain the measured inner-manifest digest, and invoke the explicitly labeled `--bootstrap-bundle-root` mode with the current protected source commit. Bootstrap receipts say `release-bootstrap` and cannot stand in for a `pinned-consumer` receipt. Assembly downloads both exact matrix receipts and their deterministic evidence archives and, before merging any release manifest, binds their protected commit, platform/accelerator selectors, inner-manifest digest, tool digests, zero-exit process records, and distinct output-artifact records to the signed Windows archives. Provisioning first warms the real PEFT fixture path and atomically records the effective converter interpreter closure: the virtual environment, Python executable, standard library, and applicable Python and runtime DLLs. Each closure row maps to a content-addressed object named by its SHA-256; duplicate bytes share one object, while conflicting size aliases fail closed. Before any bootstrap process runs, the workflow publishes both the manifest and complete object archive as a separate, immutable GitHub Actions artifact under a matrix-specific name; the only permitted step between snapshot creation and the probe is this exact commit-pinned protection step. The probe independently re-hashes that object archive, re-hashes the live closure before and after fixture creation, around every subprocess, and once more before receipt publication. Its evidence ZIP streams every unique closure object under `runtime/interpreter-closure-objects/<sha256>` and also retains immutable, step-specific snapshots of each process input immediately before execution and each output immediately after success, together with argv/cwd associations and process-result digests. Later steps cannot overwrite or relabel earlier snapshots: the probe revalidates prior outputs, and assembly independently verifies every snapshot member, reused-input identity, and producer-to-consumer byte transition. Assembly separately downloads the protected pre-execution closure artifacts, re-hashes every protected and evidence object, validates the exact row-to-object mapping and absence of extra or missing objects, and requires byte-for-byte manifest equality before accepting the receipt's closure, process, and artifact records. Missing, substituted, aliased, fabricated, mutated, or unbound receipts, protected authorities, closure objects, snapshots, or evidence fail assembly. Both hosted probe entry points remain importable without the Vetinari package: the converter interpreter runs fixture preparation and closure capture from the standalone script, while the assembly/bootstrap interpreter loads Vetinari runtime modules only when canonical or bootstrap bundle resolution is actually requested. Both modes create a deterministic tiny Llama base model, serialize a real PEFT LoRA adapter, reload both inputs in the exact locked converter interpreter, and call `PeftModel.merge_and_unload()` before invoking both converters, generating an imatrix and quantizing the GGUF. Missing authority, dependencies, signatures, process results, or receipt fields fail closed; a simulated tool, fabricated pin, manual tensor mutation, repository fallback, bootstrap receipt presented as consumer proof, or hash row without its retained bytes is not acceptable evidence.

## Supply-chain gates

The workflow applies complementary controls. `cargo deny check licenses bans sources --config deny.toml` enforces the repository-wide license, duplicate/ban, and source policy. The SPDX and attribution checks require exact Python, converter, and Cargo package identities, versions, explicitly approved or conditional SPDX expressions, package URLs, and dependency relationships; unknown, unclassified, restricted, or unresolved bundled license evidence fails closed. `scripts/check_converter_lock.py` resolves the exact Windows and `manylinux_2_28_x86_64` environments afresh with pinned uv, compares each active set against the committed union, permits PyPI as the sole general index, and permits only the two exact hash-fragmented PyTorch CPU wheel URLs. It then proves every resolved row has exact hashes and license evidence. `scripts/check_rustsec_package.py --package amw-engine` resolves the locked normal/build dependency closure for the shipped engine and intersects it with `cargo audit --json` findings. A vulnerability or warning reachable from that closure fails the job; unrelated workspace packages and dev-only dependencies do not make the engine release gate report a false failure.

The RustSec checker fails closed if Cargo metadata, the audit command, or either JSON schema is unavailable or malformed. Every package, workspace member, resolve node, dependency row, dependency-kind row, and fallback dependency identity is validated even when it is outside the selected package's reachable closure. Package and node identities must match globally, every graph reference must resolve, and each node's structured `deps` multiset must equal its fallback `dependencies` multiset; malformed unreachable rows cannot disappear through filtering. The checker writes an explicit `error` report on infrastructure/schema failure, and CI validates and uploads the report under `always()` so failed gates retain their diagnosis. Operator tests prove the reachable, unrelated, dev-only, missing-package, graph-parity, unknown-reference, and malformed-input branches.
