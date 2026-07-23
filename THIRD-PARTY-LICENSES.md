# Third-Party Licenses And Attribution Ledger

> Release status: blocker evidence ledger. This root attribution
> artifact is not final legal signoff. Rows marked `release-blocking`,
> `conditional`, or `unresolved` must be closed or explicitly accepted by the
> release owner before a release artifact can claim complete license, NOTICE,
> provenance, privacy, or public/private boundary proof.

Checked in the private source repository on 2026-04-22. REM-08 refreshed model
and dataset primary-source dispositions on 2026-05-08. The public release
dispositions are maintained in `docs/reference/release-license-evidence.md`.

## Reproduction

Regenerate the dependency inventory during release evidence refreshes and after
dependency changes with:

```powershell
.venv312/Scripts/pip-licenses.exe --format=markdown --with-urls --with-license-file --output-file THIRD-PARTY-LICENSES.generated.md
```

Compare the generated inventory against this ledger before claiming complete
third-party license closure.

## Project License

| Surface | Evidence | Disposition |
| --- | --- | --- |
| Vetinari package metadata | `pyproject.toml` declares `license = "MIT"`. | Root `LICENSE` contains MIT text. |
| Root NOTICE state | `NOTICE` exists and uses the same copyright holder as `LICENSE`. | Package NOTICE is aligned with root license text; third-party attribution remains governed by this ledger. |
| Root third-party ledger state | `THIRD-PARTY-LICENSES.md` was absent before Session 34H. | Status: open; created this release-blocking ledger and keep it active until release-owner signoff closes every unresolved row. |

## Python Dependency Ledger

The active virtualenv does not include `pip-licenses` or `pip-audit`, so this
table is a direct-dependency ledger from `pyproject.toml` plus installed package
metadata from `importlib.metadata`. It is not a complete transitive license
closure.

| Group | Requirement | Installed evidence | License evidence | Release disposition |
| --- | --- | --- | --- | --- |
| runtime | `requests>=2.28.0` | 2.33.1 | Apache-2.0 metadata | compatible; attribution required |
| runtime | `httpx>=0.27.0` | 0.28.1 | BSD-3-Clause metadata | compatible; attribution required |
| runtime | `urllib3>=1.26.0` | 2.6.3 | `License-Expression: MIT`; license file `urllib3-2.6.3.dist-info/licenses/LICENSE.txt` | compatible; attribution required |
| runtime | `pyyaml>=6.0` | 6.0.3 | MIT metadata | compatible; attribution required |
| runtime | `apscheduler>=3.10.0` | 3.11.2 | MIT metadata | compatible; attribution required |
| runtime | `psutil>=5.9.0` | 7.2.2 | BSD-3-Clause metadata | compatible; attribution required |
| runtime | `pydantic>=2.0` | 2.13.3 | `License-Expression: MIT`; license file `pydantic-2.13.3.dist-info/licenses/LICENSE` | compatible; attribution required |
| runtime | `pydantic-settings>=2.0` | 2.13.1 | MIT classifier | compatible; attribution required |
| runtime | `huggingface-hub>=0.36,<2.0` | 0.36.2 | Apache metadata | compatible; attribution required; Hub 1.x training-path proof remains release-blocking |
| runtime | `rich>=13.0` | 14.3.3 | MIT metadata | compatible; attribution required |
| runtime | `structlog>=24.0` | 25.5.0 | Apache/MIT classifiers | compatible; attribution required |
| runtime | `litestar>=2.12` | 2.21.1 | MIT metadata | compatible; attribution required |
| runtime | `msgspec>=0.18.0` | 0.21.1 | `License-Expression: BSD-3-Clause`; license file `msgspec-0.21.1.dist-info/licenses/LICENSE` | compatible; attribution required |
| runtime | `uvicorn>=0.30` | 0.43.0 | `License-Expression: BSD-3-Clause`; license file `uvicorn-0.43.0.dist-info/licenses/LICENSE.md` | compatible; attribution required |
| runtime | `asgiref>=3.7` | 3.11.1 | BSD-3-Clause metadata | compatible; attribution required |
| runtime | `stamina>=24.0.0` | 25.2.0 | MIT classifier | compatible; attribution required |
| runtime | `defusedxml>=0.7.0` | 0.7.1 | Python Software Foundation License metadata | compatible; attribution required |

## Optional Dependency Risk Ledger

| Surface | Evidence | Release disposition |
| --- | --- | --- |
| Cloud providers | `pyproject.toml` optional `cloud` group plus `config/cloud_providers.yaml` names Anthropic, OpenAI, Google Gemini, Cohere, Hugging Face Inference, and Replicate endpoints. | compatible package licenses must still be verified transitively; user prompt/content transfer requires privacy disclosure and consent. |
| Local/model extras | `llama-cpp-python`, `vllm`, `diffusers`, `torch`, `transformers`, `onnxruntime`, `sentence-transformers`, and related ML extras are optional. | Status: open; release-blocking until transitive licenses, native binary terms, and model artifact terms are resolved. |
| Guardrails extras | `nemoguardrails` and `llm_guard` are declared but not installed in this virtualenv. | unresolved before release. |
| Training extras | `datasets`, `peft`, `trl`, `bitsandbytes`, `unsloth>=2026.5.9,<2027.0; sys_platform != 'darwin' or platform_machine != 'arm64'`, and related packages are optional and partly installed. The checked environment still has `unsloth` 2025.11.1 installed with `License-Expression: Apache-2.0`, which is useful evidence but does not satisfy the current declared floor. | Status: open; package license for the observed `unsloth` install is no longer unknown/unbounded; native binary, auto-install, dataset redistribution terms, and current-floor reinstall proof remain release-blocking for training extras. |
| Notifications extras | `desktop-notifier` is declared in the optional `notifications` group. The Python 3.14 validation environment reports `desktop-notifier` 6.2.0 with `MIT` license metadata and `LICENSE`; the repo venv may omit the optional package. `pystray` 0.19.5 is optional and reports LGPLv3 metadata with `COPYING` and `COPYING.LGPL`. | `desktop-notifier` remains conditional until installer inclusion is decided; `pystray` is conditional and requires installer/linking review before shipping a bundled binary. |
| Developer tooling | pytest, build, ruff, mypy, pyright, vulture, and peers are dev-only when excluded from release artifacts. | verify dev-only boundary and transitive obligations before publishing source bundles. |

## Browser Asset And Frontend Ledger

The legacy Jinja templates under `ui/templates/` are included as release
inputs. Their CDN references are therefore tracked as browser assets even when
the current Svelte app is the primary UI.

| Surface | Evidence | Release disposition |
| --- | --- | --- |
| Google Fonts Inter | `ui/templates/index.html` and `ui/templates/dashboard.html` load `https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap` and preconnect to `https://fonts.gstatic.com`. | Status: open; Inter is released under SIL Open Font License 1.1; CDN use is release-blocking if these templates remain active without disclosure, CSP review, and privacy review. |
| Font Awesome Free 6.4.0 | `ui/templates/index.html` and `ui/templates/dashboard.html` load `https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css`. | Font Awesome Free uses icon fonts under SIL OFL 1.1 and code under MIT; attribution/disclosure required if shipped as an active UI dependency. |
| highlight.js 11.9.0 | `ui/templates/index.html` loads `https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css`. | BSD-3-Clause; attribution/disclosure required if shipped as an active UI dependency. |
| Chart.js 4.4.0 | `ui/templates/dashboard.html` loads `https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js`. | MIT; attribution/disclosure required if shipped as an active UI dependency. |
| Svelte source dependencies | `ui/svelte/package-lock.json` records Chart.js 4.5.1 MIT, highlight.js 11.11.1 BSD-3-Clause, marked 9.1.6 MIT, Svelte 5.55.1 MIT, Vite 6.4.1 MIT, and `@sveltejs/vite-plugin-svelte` 5.1.1 MIT. | dev-only while frontend is package-excluded; attribution required if shipped. |
| Generated Svelte bundles | `ui/static/svelte/js/main.js`, `vendor-chart-*`, `vendor-hljs-*`, and `vendor-marked-*` exist in the workspace. | Status: open; generated dormant bundles are release-blocking if included without source, license, and rebuild provenance. |
| Frontend local dependency tree | `ui/svelte/node_modules/**` contains generated package state and native helper binaries such as esbuild variants. | remove-before-release. |

## Model, Dataset, And Tool Provenance Ledger

Detailed public rows live in `docs/reference/release-license-evidence.md`.

| Surface | Evidence | Release disposition |
| --- | --- | --- |
| Local model artifacts | Workspace contains `ui/svelte/models/model-00001-of-00016.safetensors` around 3.99 GB. `MANIFEST.in` now prunes `ui/svelte/models` and globally excludes model binary suffixes. | Status: open; not deleted and still release-blocking for public export or installer packaging unless the release manifest/check proves exclusion or exact source/license/revision/hash/redistribution terms are assigned. |
| GGUF/model download recommendations | `vetinari/cli_packaging_data.py`, `vetinari/cli_packaging_models.py`, and setup wizard paths reference Hugging Face repos, GGUF filenames, mutable `revision="main"`, and post-download hash display. | Status: open; release-blocking until model card, base model license, immutable revision, expected digest, and attribution are recorded. |
| External datasets | `vetinari/training/external_data.py` and `data_seeder.py` list Hugging Face datasets including The Stack, Code Contests, MBPP, APPS, Competition Math, SmolTalk, Alpaca, OpenOrca, HH-RLHF, and UltraFeedback. | Status: open; release-blocking until dataset license, source, privacy class, consent, retention, and redistribution status are recorded per dataset. |
| Tool execution surfaces | Docker/Jaeger/SearXNG helpers, MCP `npx -y`, moving `uvx` package invocations, and training auto-install flows execute or fetch third-party artifacts. | Status: open; release-blocking unless pinned, allowlisted, isolated, and disclosed as dev-only or supported. |
| Fish Speech TTS | `config/knowledge/model_families.yaml` records `fishaudio/fish-speech-1.5` as `CC-BY-NC-SA-4.0`; the recommender catalog now marks it `blocked:cc-by-nc-sa-4.0`. | non-commercial; not eligible for default release/commercial recommendation without a separate commercial license and attribution review. |
| Llama Embed Nemotron | `config/knowledge/model_families.yaml` records the Nemotron embedding family as `Llama 3 Community License`; the recommender catalog now marks `nvidia/llama-embed-nemotron-8b` as `custom:llama-3-community-license`. | conditional; release claims must carry Llama 3 license fields and any gated-access acceptance record. |
| Devstral Small 2 | `config/backend_pins.yaml`, `config/agent_model_defaults.yaml`, and the recommender catalog reference `mistralai/Devstral-Small-2-24B-Instruct-2512`. | blocked for release promotion until Mistral model terms, image terms, attribution, and redistribution posture are reviewed. |
| Previously `see upstream` active recommender families | FLUX.2 dev, HunyuanVideo 1.5, Hunyuan3D 2.5, and Canary-Qwen now carry explicit blocked/custom license refs in `config/knowledge/model_families.yaml` and recommender metadata. | not default-release eligible unless a release owner records compatible commercial terms or removes the recommendation. |

REM-08 current-source rule: model and dataset entries with missing,
non-commercial, custom, gated, `other`, or inaccessible primary-source license
metadata are blocked from release/default promotion. The 2026-05-08 check
blocked `Qwen/Qwen3-8B-Instruct`, `bigcode/the-stack-v2-dedup`,
`codeparrot/codecontests`, `HuggingFaceTB/smoltalk`, `tatsu-lab/alpaca`, and
`argilla/ultrafeedback-binarized-preferences` pending exact source/license or
non-commercial/internal-only disposition.

## Focused FSA Remediation Evidence

| FSA ID | Surface | Current release disposition |
| --- | --- | --- |
| FSA-00117 | Local `.safetensors` model artifact under `ui/svelte/models` | Excluded from sdist by `MANIFEST.in`; not deleted from the workspace. Public export or installer release remains blocked unless exclusion is re-proved on the built artifact. |
| FSA-00118 | `tatsu-lab/alpaca` | `blocked:cc-by-nc-4.0-noncommercial`; not eligible for default/commercial release training. |
| FSA-00119 | `unsloth` training extra | Version-bounded as `unsloth>=2026.5.9,<2027.0; sys_platform != 'darwin' or platform_machine != 'arm64'`; the older installed 2025.11.1 package resolves to Apache-2.0 but does not prove the current floor. Training extra release remains conditional on current-floor reinstall proof and the rest of the native/optional dependency review. |
| FSA-00120 | The Stack, Code Contests, SmolTalk, UltraFeedback, and other unresolved dataset entries | Blocked or review-required entries must stay out of default training and release claims until exact license, privacy, and redistribution evidence is present. |
| FSA-00121 | Devstral Small 2 model references | No release approval is claimed; backend pins and model defaults now carry explicit blocked release-license review metadata. |
| FSA-00122 | Llama 3.1 cloud model references | Release claims that mention Llama 3.x must carry structured model license fields; the current ledger schema fails closed when those fields are absent. |
| FSA-00123 | Runtime dependencies with empty legacy `License` metadata | `License-Expression` and bundled license-file evidence now resolves `urllib3`, `pydantic`, `msgspec`, and `uvicorn`. |
| FSA-00124 | `pystray` optional notification extra | Conditional LGPLv3 optional dependency; bundled binary/installer distribution needs explicit linking review. |
| FSA-00125 | Root NOTICE mismatch | `NOTICE` and `LICENSE` now use the same copyright holder. |
| FSA-00126 | Frontend CDN/font/icon/script references | Status: open; external browser assets remain release-blocking if those templates are mounted or shipped as active UI; no vendored-asset completeness claim is made. |
| FSA-00127 | `Anthropic/hh-rlhf` privacy review | The source catalog now keeps this privacy-review-bearing dataset out of default training flows. |
| FSA-00128 | Missing root SPDX/SBOM artifact | Root `spdx.json` is generated by `scripts/generate_spdx_sbom.py`; validate with `python scripts/generate_spdx_sbom.py --check`. |
| FSA-00129 | `desktop-notifier` optional dependency metadata | The optional dependency is named in this ledger; Python 3.14 validation captured `desktop-notifier` 6.2.0, `MIT`, and `LICENSE` metadata, while the repo venv can still omit the optional package. It remains conditional until installer inclusion is decided. |
| FSA-00413 | Fish Speech recommender license | Non-commercial model metadata is explicit and default recommendation is gated by the model recommender license filter. |
| FSA-00414 | Llama Embed Nemotron recommender license | Llama 3 Community License metadata is explicit and default recommendation is gated by the model recommender license filter. |
| FSA-00415 | Active recommender `see upstream` license family | Known non-commercial/custom families now use explicit blocked/custom license refs instead of `see upstream`. |
| FSA-00290 | Release-blocking third-party rows | Status: open; preserved intentionally, and release certification must fail until blockers are either closed by evidence or accepted by an explicit release-owner decision. |

## Rust / Cargo Dependency Ledger

Transitive Rust crates bundled by the `src-tauri` Tauri desktop application.
License evidence is from crate metadata in `src-tauri/Cargo.toml` and
`crates.io` published crate metadata.

| Crate | Version constraint | License | Release disposition |
| --- | --- | --- | --- |
| `serde` | workspace | MIT | compatible; attribution required |
| `serde_json` | workspace | MIT OR Apache-2.0 | compatible; attribution required |
| `tauri` | workspace | MIT | compatible; attribution required |
| `tauri-build` | workspace | MIT | compatible; attribution required |
| `pretty_assertions` | workspace | MIT | dev-only (build/test dep); attribution required if shipped |

## Privacy And Data-Flow Ledger

Detailed public rows live in `docs/reference/privacy-manifest.json`.

| Surface | Classification | Release disposition |
| --- | --- | --- |
| Prompts, completions, project files, attachments, exports, traces, logs, memory, training data, feedback, generated authority, tool outputs, cloud provider calls, webhooks, and model/catalog identities | privacy-bearing by default | Status: open; release-blocking unless redaction, access scope, retention/delete, export, and disclosure are proven for each retained flow. |
