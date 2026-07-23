---
name: Intelligent Refactoring
description: AST-aware refactoring using LSP rename, find-references, and call-hierarchy for safe comprehensive changes
mode: build
agent: worker
version: "1.0.0"
capabilities:
  - refactoring
  - feature_implementation
tags:
  - build
  - refactoring
  - ast
  - lsp
---

# Intelligent Refactoring

## Purpose

Intelligent Refactoring performs structural code transformations using AST-aware tools (LSP rename, find-references, call-hierarchy, ast-grep) rather than text-based search-and-replace. This ensures that refactoring operations are comprehensive (catch all references, including type annotations and string literals), safe (preserve semantics, do not break callers), and verifiable (every change can be traced to a refactoring rule). It prevents the common failure mode where a "simple rename" misses references in tests, configs, or documentation, leaving the codebase in an inconsistent state.

## When to Use

- When renaming functions, classes, methods, or variables across the codebase
- When extracting common logic into shared utilities
- When moving functions or classes between modules
- When simplifying complex conditional logic or deep nesting
- When converting between patterns (e.g., inheritance to composition)
- When reducing code duplication identified by the Inspector
- When changing function signatures and updating all callers

## Inputs

| Parameter       | Type            | Required | Description                                                        |
|-----------------|-----------------|----------|--------------------------------------------------------------------|
| task            | string          | Yes      | What to refactor and why                                           |
| files           | list[string]    | No       | Files to refactor                                                  |
| refactoring_type| string          | No       | Type: "rename", "extract", "move", "inline", "simplify"           |
| context         | dict            | No       | Codebase patterns, scope analysis results                         |
| thinking_mode   | string          | No       | Thinking budget tier                                               |

## Process Steps

1. **Scope determination** -- Use LSP find-references to identify all locations affected by the refactoring. This includes: direct calls, imports, type annotations, docstring references, test assertions, and configuration values.

2. **Safety analysis** -- Assess the refactoring risk:
   - **Rename**: Safe if all references are found (LSP covers code, grep covers strings/configs)
   - **Extract**: Safe if extracted code has no side effects on shared state
   - **Move**: Risky if circular import potential exists
   - **Inline**: Safe only if the inlined function is called from one place
   - **Simplify**: Risky if simplification changes subtle behavior

3. **Dry run** -- For AST-based transformations (ast-grep), always run with `dryRun=true` first. Review the proposed changes before applying. For LSP rename, preview all affected locations before confirming.

4. **Transformation execution** -- Apply the refactoring in a specific order:
   - For rename: LSP rename handles code; manual update for strings, configs, docs
   - For extract: copy code to new location, replace original with call, update imports
   - For move: update source module (remove), target module (add), all importers (update paths)
   - For simplify: apply transformation, verify behavior preservation with tests

5. **Import chain repair** -- After any move or extract operation, verify the import chain:
   - No circular imports created
   - All `__init__.py` files updated for re-exports
   - No dangling imports (importing something that was moved)

6. **Test verification** -- Run the full test suite for renamed/moved symbols. Run targeted tests for extracted/simplified code. Verify that test count has not decreased (no tests accidentally broken or removed).

7. **Documentation update** -- Update docstrings, comments, and markdown documentation that reference renamed or moved symbols. This includes AGENTS.md, CLAUDE.md, and any architecture docs.

8. **Consistency check** -- After refactoring, grep the entire codebase for the old name/pattern to verify no references were missed. Check strings, comments, YAML configs, and documentation.

## Output Format

The skill produces a refactoring report:

```json
{
  "success": true,
  "output": "Renamed MemoryType.WARNING to MemoryType.RISK_WARNING across 8 files",
  "files_changed": [
    "vetinari/types.py (enum value renamed)",
    "vetinari/memory/unified.py (3 references updated)",
    "vetinari/memory/_schema.py (2 references updated)",
    "tests/test_memory_store.py (4 test references updated)"
  ],
  "warnings": [
    "Manual update needed: docs/reference/memory.md references the old enum name in prose"
  ],
  "metadata": {
    "references_found": 42,
    "references_updated": 42,
    "files_scanned": 150,
    "dry_run_passed": true,
    "tests_passed": "all 245 tests pass"
  }
}
```

## Quality Standards

This skill is governed by the following standards from the skill registry:

- **STD-WRK-011**: Build mode is the SOLE writer of production files
- **STD-WRK-012**: All new code MUST have type annotations, Google-style docstrings, and tests
- **STD-WRK-013**: Imports MUST use canonical sources
- **STD-WRK-016**: No TODO, FIXME, pass bodies, or placeholder strings
- **STD-UNI-001**: All skill outputs MUST conform to the declared output_schema
- **CON-WRK-004**: Build mode is the SOLE production file writer
- **CON-WRK-005**: Destructive operations require confirmation

## Examples

### Example: Extract common validation logic

**Input:**
```
task: "Extract duplicated input validation from 4 API endpoints into a shared validator"
files: ["vetinari/web/projects_api.py", "vetinari/web/training_routes.py"]
refactoring_type: "extract"
```

**Output (abbreviated):**
```
success: true
files_changed:
  - vetinari/web/validators.py (new - shared validation functions)
  - vetinari/web/projects_api.py (4 endpoints updated to use shared validator)
  - vetinari/web/training_routes.py (2 endpoints updated to use shared validator)
  - vetinari/web/__init__.py (updated - validators module registered)
  - tests/test_validators.py (new - 12 tests for shared validators)

refactoring_details:
  - "Extracted validate_project_id(), validate_pagination(), validate_json_body()"
  - "6 call sites updated to use shared functions"
  - "Original validation code removed (no duplication)"
  - "Net reduction: -45 lines duplicated code, +30 lines shared module"
```
