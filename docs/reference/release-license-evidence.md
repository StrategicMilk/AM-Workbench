# Release License Evidence

This page records the release/licensing evidence used for the focused FSA
remediation slice. It is evidence for blockers and gates, not legal signoff.

## Release Rule

A public or packaged release can claim license/publication readiness only after
all of these are true for the exact artifact being shipped:

| Gate | Required proof |
|---|---|
| Release claims ledger | `python scripts/check_release_claims_ledger.py --release-root outputs/release` passes for every checked ledger. |
| Source distribution contents | `python scripts/check_sdist_contents.py --manifest MANIFEST.in` passes, and the built sdist contains no private trees or model binaries. |
| Public export boundary | `python scripts/build_public_export.py <outside-target> --source <private-root> --force` produces `PUBLIC_EXPORT_MANIFEST.json`, then `python scripts/check_publication_boundary.py --export-root <outside-target>` passes. |
| License blockers | `THIRD-PARTY-LICENSES.md` has no unresolved, conditional, or release-blocking row unless the release owner records an explicit decision. |
| Version tag | `python scripts/check_ci_release_proof.py` validates `docs/reference/release-tag-evidence-v0.6.0.json` against `git tag --list v0.6.0`. The private checkout currently has no `v0.6.0` tag, so the evidence records a release blocker rather than claiming a release. |

## Evidence Notes

The following rows are intentionally conservative. They distinguish "excluded
from the current source distribution manifest" from "legally approved for
redistribution."

| Surface | Evidence | Release disposition |
|---|---|---|
| Local `.safetensors` model files | `MANIFEST.in` prunes `ui/svelte/models` and globally excludes model binary suffixes. | Not deleted. Exclusion must be re-proved against the built sdist and public export before release. |
| Training datasets | `vetinari/training/external_data.py` records blocked `license_ref` values for Alpaca, The Stack, SmolTalk, Code Contests, and UltraFeedback. | Blocked or review-required datasets must not support default release claims. |
| HH-RLHF | The catalog marks `Anthropic/hh-rlhf` as `mit:privacy-review-required` and keeps `default_training_allowed` false. | Privacy-review-bearing data stays out of default training/release flows until a privacy review records an explicit approval. |
| Runtime license metadata | Installed package metadata exposes `License-Expression` for `urllib3`, `pydantic`, `msgspec`, and `uvicorn`. | Direct runtime rows are resolved in `THIRD-PARTY-LICENSES.md`; transitive closure and attribution packaging still need a release gate. |
| `unsloth` training extra | `pyproject.toml` keeps normal `training` on the vanilla stack and uses `training-unsloth` for `unsloth>=2026.5.9,<2027.0` outside Darwin arm64; the checked environment still has `unsloth` 2025.11.1 installed and that older package reports Apache-2.0. | The optional accelerated training extra is version-bounded and mutually exclusive with serving/image aggregate extras, but release proof must reinstall and verify the current floor before claiming accelerated-training readiness. Native binaries and auto-install paths still need review. |
| TRL training extra | PyPI package metadata checked on 2026-05-21 reports latest `trl` 1.4.0 and local metadata reports installed 0.23.0. `pyproject.toml` now uses `trl>=0.24,<0.25` in both vanilla and accelerated training profiles. | The stale `<0.20` bound is removed. 0.25+ remains blocked until the training scripts and Unsloth compatibility are migrated and verified. |
| Transformers training/image extras | PyPI package metadata checked on 2026-05-21 reports latest `transformers` 5.9.0 and local metadata reports installed 4.57.2. `pyproject.toml` keeps image, ComfyUI, and vanilla `training` extras on `transformers>=4.57,<5.0`, while `training-unsloth` uses `transformers>=5.1,<5.5` to match the current Unsloth dependency graph. | Fresh image/ComfyUI/vanilla training installs cannot silently cross the unvalidated 5.x major boundary; accelerated training installs must be re-proved against the 5.x path before claiming release readiness. |
| Hugging Face Hub runtime/training extra | PyPI package metadata checked on 2026-05-21 reports latest `huggingface-hub` 1.15.0 and local metadata reports installed 0.36.2. `pyproject.toml` now uses `huggingface-hub>=0.36,<2.0` across base, vanilla training, and accelerated training profiles. | Hub 1.x compatibility is resolver-enabled, but training/release proof must reinstall and validate the 1.x path before claiming training-extra readiness. |
| Python and APScheduler horizon | The Python devguide status page checked on 2026-05-21 lists Python 3.11 EOL as 2027-10; package metadata reports APScheduler 3.11.2 installed and latest. | The prior "Python 3.11 EOL October 2026" claim is not current; Python 3.11 remains a supported floor, while CI also covers 3.12 and 3.13. |
| `pystray` notification extra | `pystray` is optional and reports LGPLv3 metadata. | Conditional; bundled binary installers need explicit linking/distribution review. |
| Llama 3.x model claims | `vetinari.release.proof_schema` requires structured model license fields for Llama 3.x release claims. | Ledger claims fail closed if Llama 3.x license fields are missing. |
| Devstral model references | `config/backend_pins.yaml` and `config/agent_model_defaults.yaml` reference Devstral Small 2 and carry `review-required:mistral-ai-model-license` release metadata. | No release approval is claimed; model-card/license review remains required. |
| Frontend CDN templates | `ui/templates/index.html` and `ui/templates/dashboard.html` reference Google Fonts, Font Awesome/cdnjs, highlight.js/cdnjs, and Chart.js/jsDelivr. | Release-blocking if those templates are mounted or shipped as active UI without vendoring, attribution, CSP, and disclosure. |
| SPDX/SBOM | `spdx.json` is generated by `scripts/generate_spdx_sbom.py` and validated by `python scripts/generate_spdx_sbom.py --check`. | Minimal SPDX 2.3 SBOM exists for direct dependency release proof; transitive closure remains release-blocking until a full resolver-backed SBOM is produced. |
| Restricted recommender models | Fish Speech, Llama Embed Nemotron, Devstral Small 2, FLUX.2 dev, HunyuanVideo 1.5, Hunyuan3D 2.5, and Canary-Qwen carry explicit blocked/custom/review-required license refs in recommender or config metadata. | These are not default-release approvals; the recommender license filter prevents blocked/custom rows from default promotion where it selects active recommendations. |

## Browser CDN Asset Evidence

| Template asset | Source reference | License/disposition |
|---|---|---|
| Google Fonts Inter | `https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap`; preconnect to `https://fonts.gstatic.com` | SIL OFL 1.1 font; CDN use stays release-blocking for active templates until disclosure, CSP, and privacy review are complete. |
| Font Awesome Free 6.4.0 | `https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css` | SIL OFL 1.1 for icon fonts and MIT for code; attribution/disclosure required if active. |
| highlight.js 11.9.0 | `https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css` | BSD-3-Clause; attribution/disclosure required if active. |
| Chart.js 4.4.0 | `https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js` | MIT; attribution/disclosure required if active. |
