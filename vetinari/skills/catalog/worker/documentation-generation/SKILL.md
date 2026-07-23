---
name: Documentation Generation
description: Generate API docs, architecture docs, and changelogs with Google-style docstrings and properly structured markdown
mode: documentation
agent: worker
version: "1.0.0"
capabilities:
  - documentation_generation
  - creative_writing
tags:
  - operations
  - documentation
  - api-docs
  - changelog
---

# Documentation Generation

## Purpose

Documentation Generation produces structured, accurate documentation from code analysis and project context. It covers API documentation (endpoint references, function signatures), architecture documentation (component diagrams, data flow descriptions), changelogs (user-facing impact summaries), and inline documentation (Google-style docstrings, module docstrings). The output follows strict formatting rules and is specific and actionable -- never vague placeholders like "handle appropriately" or empty sections.

## When to Use

- After a major feature is implemented and needs documentation
- When API endpoints change and reference docs need updating
- When the architecture evolves and docs need to reflect the new structure
- When generating changelog entries for a release
- When the Inspector flags missing or inadequate docstrings
- When onboarding documentation needs updating after significant codebase changes
- When existing documentation has drifted from the actual code

## Inputs

| Parameter       | Type            | Required | Description                                                        |
|-----------------|-----------------|----------|--------------------------------------------------------------------|
| task            | string          | Yes      | What to document and the documentation objective                   |
| doc_type        | string          | No       | Type: "api", "architecture", "changelog", "docstrings", "guide"   |
| files           | list[string]    | No       | Source files to document                                           |
| changes         | list[dict]      | No       | Recent changes to include in changelog                             |
| audience        | string          | No       | Target audience: "developer", "user", "admin"                     |
| context         | dict            | No       | Project context, existing docs, style guide                       |

## Process Steps

1. **Source analysis** -- Read the code or system being documented. Extract: function signatures, class hierarchies, module purposes, data flows, configuration options, and error conditions. Documentation must be derived from actual code, not assumptions.

2. **Structure planning** -- Choose the appropriate document structure based on doc_type:
   - **API docs**: endpoint table, request/response schemas, examples, error codes
   - **Architecture docs**: component overview, data flow, deployment, technology choices
   - **Changelog**: grouped by type (Added, Changed, Fixed, Removed, Security)
   - **Docstrings**: Google-style with Args, Returns, Raises, Examples sections
   - **Guide**: narrative with code examples, step-by-step instructions

3. **Content generation** -- Write the documentation following project conventions:
   - ATX-style headers (`#`, `##`, `###`)
   - Fenced code blocks with language identifiers
   - Tables for comparisons and parameter lists
   - Lists using `-` for unordered, `1.` for ordered
   - Cross-references using relative paths
   - Every section has meaningful content (no empty sections)

4. **Accuracy verification** -- Cross-check every claim in the documentation against the code:
   - Function signatures match actual code
   - Parameter types match type annotations
   - Return values match actual returns
   - Error conditions match actual raises
   - Code examples are syntactically correct and runnable

5. **Completeness check** -- Verify all public APIs are documented:
   - Every public function has a docstring
   - Every endpoint has request/response documentation
   - Every configuration option is documented with defaults
   - Every error code has an explanation

6. **Style enforcement** -- Apply documentation style rules:
   - Docstrings are meaningful (>10 characters, not restating the name)
   - Functions with 2+ params have Args sections
   - Functions with returns have Returns sections
   - Functions that raise have Raises sections
   - No vague language ("do the right thing", "handle appropriately")

7. **Example creation** -- Write concrete examples for each documented API:
   - Show real-world usage, not trivial cases
   - Include input and expected output
   - Cover both happy path and error handling
   - Code examples follow project conventions

8. **Cross-reference linking** -- Add links between related documentation:
   - API docs link to architecture docs for design context
   - Architecture docs link to ADRs for decision rationale
   - Changelogs link to relevant documentation updates

## Fail-Closed Evidence Contract

Documentation generation may describe completed implementation work, but it must
refuse terminal success when the implementation proof is stale, missing,
contradictory, or unreadable. A closure-ledger-only proof is insufficient:
terminal documentation output requires source/test/config/release evidence that
directly supports the documented claim.

When running as a post-execution operations mode, documentation generation must
not modify already-reviewed code; in practical terms, do not modify
already-reviewed code. If the requested documentation would need code changes
to become accurate, return a blocker that names the missing
source/test/config/release evidence instead of inventing or softening the claim.

Required fail-closed signals:

- Treat stale, missing, contradictory, or unreadable implementation evidence as a blocker.
- In operations mode, do not modify already-reviewed code.

## Output Format

The skill produces documentation content:

```json
{
  "success": true,
  "output": "Generated API documentation for 8 endpoints in vetinari/web/",
  "files_changed": [
    "docs/api-reference.md (updated - 8 endpoints documented)",
    "CHANGELOG.md (updated - added v0.5.0 entries)"
  ],
  "metadata": {
    "endpoints_documented": 8,
    "docstrings_written": 12,
    "examples_included": 6,
    "word_count": 2400,
    "evidence_contract": "fail-closed"
  },
  "provenance": [
    {
      "evidence_type": "source/test/config/release",
      "reference": "tests/api/test_projects_api.py::test_project_endpoint_contract"
    }
  ]
}
```

## Quality Standards

This skill is governed by the following standards from the skill registry:

- **STD-WRK-017**: Documentation mode output MUST have clear section structure with table of contents for >3 sections
- **STD-WRK-011**: Build mode is the SOLE writer of production files (docstring updates count as production code changes)
- **STD-UNI-001**: All skill outputs MUST conform to the declared output_schema
- **STD-UNI-003**: All skill executions MUST log entry and exit at INFO level with timing
- **CON-WRK-006**: Maximum output length for documentation generation: 10000 words
- **CON-WRK-009**: Operations modes run post-execution -- MUST NOT modify already-reviewed code
- **GDL-WRK-007**: Run documentation mode after every major feature completion

## Examples

### Example: Generating changelog entries

**Input:**
```
task: "Generate CHANGELOG.md entries for the v0.5.0 release"
doc_type: "changelog"
changes: [
  {type: "added", description: "Rate limiting middleware for REST API"},
  {type: "fixed", description: "Blackboard race condition during concurrent reads"},
  {type: "changed", description: "Agent pipeline uses 3-agent factory model (Foreman/Worker/Inspector)"}
]
```

**Output:**
```markdown
## [0.5.0] - 2025-01-15

### Added
- Rate limiting middleware for the REST API using token bucket algorithm with configurable per-endpoint thresholds

### Changed
- Agent pipeline consolidated to 3-agent factory model (Foreman, Worker, Inspector) per ADR-0061

### Fixed
- Race condition in blackboard concurrent reads that caused intermittent KeyError under load
```
