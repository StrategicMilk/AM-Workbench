# Changelog

All notable changes to AM Workbench are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Inline `[ledger:<id>]` tags are historical internal traceability labels. They
are not release-ledger foreign keys and must not be used as proof that
`outputs/release/<version>/ledger.jsonl` contains a matching record. Current
release proof comes from the verification commands named in `ROADMAP.md`.
Verification: `.venv312/Scripts/python.exe -c "t=open('CHANGELOG.md',encoding='utf-8').read(); assert '[ledger:' in t and 'release-ledger foreign keys' in t"`

---

## [Unreleased]

### Changed

- Removed dead optional dependency pins for `outlines`, `faiss-cpu`, and
  `lancedb`; `cpu_tier` is no longer included in the GPU or all aggregate
  extras.

### Verification Notes

- Current VET status is command-scoped, not inferred from historical Wave 30
  notes: run `python scripts/check_vetinari_rules.py`
  for the full gate or the pack-scoped `--paths` selector named in the active
  remediation plan.
- Current publication-boundary proof is not implied by earlier release notes:
  run `python scripts/check_publication_boundary.py --root %VETINARI_PUBLIC_EXPORT_ROOT%`
  against the live public export.
- License metadata scope is limited: `pyproject.toml` declares the project
  license and license files, but third-party dependency license closure still
  requires the license inventory/export workflow.
- Optional dependency license isolation is release-engineering work; do not
  treat project metadata as proof that every optional extra is isolated.
- All-extras license export remains deferred to CI/release engineering until a
  lockfile or export job records the combined dependency license set.
- Wave 21 runtime/docs follow-up closure for
  `wave3-runtime-docs-followup-01-P01` is validated by
  `scripts/check_developer_workflow_health.py --pack-slug
  wave3-runtime-docs-followup-01-P01 --json`; the changelog entry is a release
  note only, not the closure proof.

### Added

#### AM Workbench Runtime (Waves 25–29)

- Cohesion canary evaluation engine (`evaluate_cohesion_canary`) with idle/finalizer canary contracts and admin-gated API. [ledger:workbench-cohesion-canary-engine-01]
- Cohesion canary schema at `schemas/workbench_cohesion_canary.schema.json` for validating canary evidence and improvement proposal candidates. [ledger:workbench-cohesion-canary-schema-01]
- Workbench readiness layer (`ReadinessMode`, `WorkbenchReadinessSnapshot`, `evaluate_workbench_readiness`, `evaluate_workbench_admission`) with feature-gated confidence modes and admin-gated API at `/api/workbench/readiness`. [ledger:workbench-readiness-engine-01]
- `WorkbenchReadinessView` Svelte route and readiness policy config at `config/workbench/readiness.yaml`. [ledger:workbench-readiness-ui-01]
- Approval chain resolution engine (`ApprovalChainResolver`, `evaluate_approval_chain`, `render_approval_chain_explanation`) with ordered policy evaluation and admin-gated API at `/api/workbench/approval-chain`. [ledger:workbench-approval-chain-engine-01]
- Workbench status and health console (`WorkbenchHealthState`, `WorkbenchStatusSnapshot`, `build_workbench_status_snapshot`, `run_workbench_status_action`) with admin-gated API at `/api/workbench/status` and `WorkbenchStatusView` Svelte route. [ledger:workbench-status-console-01]
- Hardware Digital Twin advisory layer: bounded hardware probes, benchmark snapshots, state persistence, drift detection, and optimizer helpers at `vetinari/workbench/hardware/`. [ledger:workbench-hardware-twin-01]
- Channel Hub delivery engine (`vetinari.workbench.channels`) with config loading, delivery envelope building, approval routing, command routing, activity records, and media redaction; admin-gated API at `/api/workbench/channels`. [ledger:workbench-channel-hub-01]
- Update Safety subsystem (`vetinari.workbench.update_safety`) with channel policy, manifest integrity verification, skipped-version state, rollback plan generation, and redaction-first support-bundle helpers; admin-gated API at `/api/workbench/updates`. [ledger:workbench-update-safety-01]
- Command Safety subsystem (`vetinari.workbench.command_safety`) with command profile loading, classification, CWD-history state, command-decision service, receipt emission, and tool-pin/CWD gates; admin-gated API at `/api/workbench/command-safety`. [ledger:workbench-command-safety-01]
- Workflow Builder package (`vetinari.workbench.workflow_builder`) exposing `WorkflowGraph`, `WorkflowBuilderStore`, `validate_workflow_graph`, and `build_workflow_preview`; admin-gated API at `/api/workbench/workflow-builder`; `WorkbenchWorkflowBuilderView` Svelte route. [ledger:workbench-workflow-builder-01]
- Network Transport Optimizer (`vetinari.workbench.network`) exposing `NetworkObservation`, `NetworkTransportPolicy`, `NetworkTransportStateStore`, and `optimize_network_transport` as advisory-only route decisions. [ledger:workbench-network-transport-01]
- Adaptive Tuning package (`vetinari.workbench.adaptive_tuning`) with contracts, config, policy, signal normalization, store, engine, and adapter exports; admin-gated API at `/api/workbench/adaptive-tuning/*`; `AdaptiveTuningView` Svelte route. [ledger:workbench-adaptive-tuning-01]
- Svelte views and component libraries for Approval Chain, Status, Hardware Digital Twin, Channel Hub, Update Safety, Command Safety, Workflow Builder, Network Transport, and Adaptive Tuning. [ledger:workbench-svelte-views-01]
- Runtime schemas for all new Workbench subsystems: approval chain, status, hardware digital twin, channel, command safety, update, workflow builder, network transport, and adaptive tuning. [ledger:workbench-schemas-batch-01]
- Runtime configs for all new Workbench subsystems: `config/workbench/approval_chain.yaml`, `config/workbench/status_checks.yaml`, `config/workbench/hardware_profiles.yaml`, `config/workbench/channels.yaml`, `config/workbench/command_safety.yaml`, `config/workbench/update_channels.yaml`, `config/workbench/adaptive_tuning.yaml`. [ledger:workbench-configs-batch-01]
- `installer/update_manifest.schema.json` for update manifest integrity verification. [ledger:workbench-update-manifest-schema-01]

#### Quality, Tooling, and Release Boundary (Waves 30–31)

- `tests/async_utils.py` test-support module for loop-safe awaitable execution in the test harness. [ledger:async-utils-test-support-01]
- Internal file-size split helper modules (mailbox signals, AKS bundle records, coverage models/signals, model registry support, run-kernel records) introduced by VET127 enforcement. [ledger:vet127-file-splits-01]
- `scripts/check_publication_boundary.py` extended to support non-Git export roots by walking a copied file tree. [ledger:publication-boundary-non-git-01]
- Publication boundary checker now enforces the required public export asset inventory: `README.md`, `LICENSE`, `NOTICE`, `pyproject.toml`, `requirements.txt`, `config/support_matrix.yaml`, `ui/svelte/package.json`, `ui/svelte/package-lock.json`, and generated `PUBLIC_EXPORT_MANIFEST.json`. [ledger:publication-boundary-required-assets-01]
- `scripts/build_public_export.py` now generates `PUBLIC_EXPORT_MANIFEST.json` with `source_root_redacted: true` to prevent private checkout paths from shipping in public artifacts. [ledger:public-export-manifest-redaction-01]

#### Documentation Refresh (Wave 32)

- `ROADMAP.md` rewritten as a factual post-roadmap delivery record documenting all 32 waves, Workbench-as-product-surface design principle, and public release boundary as CI gate. [ledger:roadmap-refresh-wave32-01]

### Changed

#### Inference Backends (Wave 1)

- NeMo/Riva backend stub removed from first-class backend rosters, setup recommendations, parity lanes, and project identity generation; unknown `nemo_riva` provider values now remain fail-closed until a real adapter exists. [ledger:nemo-riva-stub-removal-01]

#### AM Workbench Runtime (Waves 25–29)

- Workbench status writable actions now route through `run_workbench_status_action` backed by Approval Chain, replacing direct gateway-policy approval as an authority substitute. [ledger:workbench-status-approval-chain-wiring-01]
- Channel Hub command integrations accept `remote_intent` payloads through the API; delivery and command authorization remain separate gates. [ledger:workbench-channel-command-separation-01]
- Update Safety API rejects caller-controlled `state_path`, `output_root`, and untrusted approval-decision skips; unknown update channels fail closed. [ledger:workbench-update-safety-trust-boundary-01]
- Command Safety decisions require prior trusted CWD state and profile-root containment; blocked decisions no longer prime missing CWD state. [ledger:workbench-command-safety-cwd-hardening-01]
- Hardware Digital Twin outputs remain advisory-only adapter payloads; no live route or shell wiring added through Wave 29. [ledger:workbench-hardware-advisory-boundary-01]
- Network transport routing decisions are advisory-only and do not mutate host network settings, OS settings, or project defaults. [ledger:workbench-network-advisory-boundary-01]
- Adaptive tuning UI controls are event-only; backend mutation remains through the admin-gated API helper layer. [ledger:workbench-adaptive-tuning-ui-event-only-01]

#### Quality, Tooling, and Release Boundary (Waves 30–31)

- Historical Wave 30 evidence recorded `scripts/check_vetinari_rules.py` with `0 error(s), 0 warning(s)` on that post-Wave-30 baseline; current VET status requires the live command named in the Unreleased verification notes. [ledger:vet-rules-zero-errors-wave30-01]
- Existing workbench experiment schema title corrected to match its declared contract. [ledger:experiment-schema-title-fix-01]
- Generated JS assets encoding whitespace tables now use escaped strings to avoid trailing-whitespace diff-check failures. [ledger:js-whitespace-table-escaping-01]
- LF-attributed files remain LF in the working tree to keep `git diff --check` warning-free on Windows. [ledger:lf-attribute-windows-fix-01]
- Public exporter skips text files containing private checkout runtime references; `PUBLIC_EXPORT_MANIFEST.json` no longer contains `source_root`. [ledger:public-export-private-ref-skip-01]

### Fixed

#### AM Workbench Runtime (Waves 25–29)

- Route false-greens in Channel Hub, Update Safety, and Command Safety test suites replaced with branch-discriminating API tests. [ledger:wave27-route-false-green-fix-01]
- Channel redaction now covers `Authorization`/`Bearer` and kebab-case credential fields. [ledger:channel-redaction-coverage-fix-01]
- Command Safety CWD recovery and idempotency gaps corrected; process-level service reused across requests. [ledger:command-safety-idempotency-fix-01]
- `test_status_route_registered_in_litestar_app` corrected after parent integration exposed order-dependent monkeypatch failure. [ledger:status-route-monkeypatch-fix-01]
- Approval Chain integrated route proof and Status UI approval resolution tightened after Wave 26 parent review. [ledger:wave26-approval-status-tightening-01]
- Hardware snapshot project scoping and assistant-context redaction corrected during Wave 26 parent review. [ledger:hardware-snapshot-scoping-fix-01]

#### Quality, Tooling, and Release Boundary (Waves 30–31)

- Full pytest baseline green again across all 777 test files via 78-chunk strict sharded run after Wave 30 terminal quality cleanup. [ledger:wave30-pytest-baseline-green-01]
- Test harness cleanup now stops queue listeners and restores module parent attributes when tests mutate `sys.modules`. [ledger:test-harness-queue-cleanup-fix-01]
- Loop-safe awaitable execution replaces `asyncio.run(...)` calls inside potentially active event-loop test contexts. [ledger:async-run-in-tests-fix-01]

### Security

- Approval Chain enforced as the authority gate for all workbench writable status actions; direct gateway-policy bypass removed. [ledger:approval-chain-authority-gate-01]
- Update Safety API pins `state_path` and `output_root` server-side; caller-supplied approval decisions are rejected. [ledger:update-safety-server-side-pins-01]
- Command Safety requires profile-root CWD containment and prior trusted CWD state before issuing allow decisions. [ledger:command-safety-cwd-containment-01]
- Support-bundle output roots are pinned server-side; only safe relative destination names are accepted. [ledger:support-bundle-server-side-output-root-01]
- Publication boundary checker rejects private runtime references across all text public export assets. [ledger:publication-boundary-private-ref-rejection-01]
- Channel Hub approval evidence required before approval-required channel deliveries are executed. [ledger:channel-hub-approval-evidence-required-01]

## [0.6.0] - 2026-04-22

### Added

- Final private release signoff package, including the refreshed release verification report, evidence matrix, and signoff summary.
- Blocking CI release-proof coverage for package build/install smoke, route-auth proof, audit-prevention checks, and release-certifier wiring.
- Explicit package-boundary proof for shipped runtime assets, including `LICENSE`, `NOTICE`, bounded `vetinari/config/**` data, and clean install smoke from built artifacts.

### Changed

- Canonical release metadata now resolves to `v0.6.0` from the package version source and aligned release-bearing docs.
- The release narrative now reflects the verified current system: a three-agent factory pipeline, bounded learning/autonomy claims, no shipped browser UI surface, and blocking release-proof gates.
- Test-governance evidence now comes from the repaired full baseline, the strengthened test-quality scanner, and documented `noqa` rationales instead of historical blocker snapshots.

### Fixed

- The full pytest baseline is green again, including prior route/auth/protocol, runtime, dashboard, training, and shutdown regressions.
- `scripts/check_test_quality.py` now correctly maps package stems and reports a clean suite instead of false blocker counts.
- Packaging/install proof now succeeds from built wheel and sdist artifacts, with the installed `vetinari` wrapper exporting a real `main()` entry point.
- Release artifacts are bounded to intended package inputs rather than leaking maintainer roots, audit trees, frontend dependency trees, or model payloads.
- Visible mojibake and non-ASCII drift in release-facing CLI/runtime files were cleaned so installed help and metadata read cleanly.

### Removed

- The broken `vetinari-asgi` console-script release surface is no longer published.
- Internal maintainer-only roots and accidental packaging residues are excluded from shipped artifacts.

### Security

- Release proof now treats route-auth, degraded-health, and prevention checks as blocking evidence instead of advisory narrative.
- `noqa` suppressions are now reviewed under the dedicated suppression policy so stale or convenience-only escapes are surfaced explicitly.

## [0.5.0] - 2026-03-11

### Added

- Analytics REST endpoints for cost, SLA, anomaly, forecast, model, agent, and summary reporting.
- Tiered cascade routing, batch processing, file-based agent governance, and enriched agent registry metadata.

### Changed

- Stale legacy agent references were migrated to the consolidated agent set across code, docs, tests, and benchmarks.
- The project converged on the post-consolidation governance model that later fed the three-agent factory pipeline.

### Security

- Constant-time token verification, trusted-proxy handling, rate limiting, and stricter input validation were enforced across sensitive web routes.
- Mutating routes for sandbox, planning, ADR, decomposition, ponder, rules, and training were hardened behind auth checks.

## [0.4.0] - 2026-02

### Added

- Consolidated six-agent architecture with typed output schemas, circuit breakers, token budgets, dynamic model routing, and SQLite-backed cost tracking.
- `TwoLayerOrchestrator` as the single execution engine replacing the prior assembly-line orchestrator.

### Changed

- Agent enums and dispatch tables were migrated to the consolidated agent family.

## [0.3.0] - 2026-01

### Added

- A 22-agent multi-stage system with DAG scheduling, blackboard memory, feedback loops, prompt evolution, and a Flask dashboard.
- Structured logging, OpenTelemetry tracing, and multi-source search support.

### Changed

- The web server replaced the CLI-only interface as the primary runtime surface.

## [0.2.0] - 2025-12

### Added

- Planning engine, dual memory tiers, shared blackboard, constraints, safety package, checkpoint recovery, cost tracking, and tracing foundations.

### Changed

- Provider and runtime configuration moved out of hardcoded values and into versioned config files.

## [0.1.0] - 2025-11

### Added

- Initial LM Studio adapter, execution-context system, tool registry, provider abstraction layer, verifier pipeline, and enhanced CLI.
- Core package scaffolding for exceptions, types, contracts, and agent interfaces.

---

[Unreleased]: https://github.com/StrategicMilk/AM-Workbench/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/StrategicMilk/AM-Workbench/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/StrategicMilk/AM-Workbench/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/StrategicMilk/AM-Workbench/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/StrategicMilk/AM-Workbench/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/StrategicMilk/AM-Workbench/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/StrategicMilk/AM-Workbench/releases/tag/v0.1.0
