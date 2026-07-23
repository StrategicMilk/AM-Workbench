# UI Product Accessibility Residuals

This reference records the REM-11 UI remediation contract for configuration validation,
guardrail rendering, attribution, and supportability.

## Effective Config Contract

Effective config renderers validate returned keys against the active schema. Unknown keys
surface an `unknown-key` badge. Required schema keys that are absent surface a
`required-missing` badge.

## Attribution Table

The Svelte UI dependency attribution is generated from `ui/svelte/package.json`.
The packages below are the direct runtime and developer dependencies that must remain
represented in user-facing attribution reviews. Runtime packages and accessibility test
packages are both listed so review screens do not under-report user-impacting UI code:

- `@axe-core/playwright`
- `@playwright/test`
- `@sveltejs/vite-plugin-svelte`
- `lucide-svelte`
- `svelte`
- `vite`

## Generated Asset Accessibility

Worker `ui_design` requests must carry an explicit `accessibility_level` target.
Worker `image_generation` requests must carry `alt_text`, and generated asset
results must preserve that text next to the file path so downstream UI surfaces
can render a text alternative instead of exposing an image-only result.

## Supportability Entries

### Workbench guardrail blocked

- description: A guarded Workbench action was disabled because safety or authorization checks did not return `status: ok`.
- usage_example: Try the guarded action without an admin token or with a failing safety check.
- doc_link: `docs/troubleshooting.md#workbench-guardrail-blocked`

### Init dry-run trace redacted

- description: `init --dry-run` output shown in the UI redacts local file-system paths and model weight locations.
- usage_example: Render dry-run output that contains `C:\Users\name\.venv\weights\model.gguf`.
- doc_link: `docs/troubleshooting.md#init-dry-run-trace-redacted`
