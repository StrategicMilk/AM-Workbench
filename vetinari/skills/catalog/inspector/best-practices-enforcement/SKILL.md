---
name: Best Practices Enforcement
description: Project-specific VET rule enforcement beyond what ruff catches, covering custom Vetinari conventions
mode: code_review
agent: inspector
version: "1.0.0"
capabilities:
  - best_practices_enforcement
  - code_review
tags:
  - quality
  - enforcement
  - vet-rules
  - conventions
---

# Best Practices Enforcement

## Purpose

Best Practices Enforcement verifies code compliance with Vetinari-specific conventions that automated tools (ruff, mypy) cannot catch. While ruff enforces PEP 8 and general Python best practices, Vetinari has 31 custom rules (VET001-VET102) covering agent architecture, naming conventions, import patterns, documentation quality, and completeness requirements. This skill acts as the final check that all project-specific conventions are followed, ensuring that code is not just valid Python but idiomatic Vetinari code.

## When to Use

- As part of every code review to catch project-specific violations
- When a new contributor submits code that may not follow Vetinari conventions
- After generating code to verify it matches project patterns
- When the automated check_vetinari_rules.py reports violations that need explanation
- When reviewing changes to shared modules (types.py, contracts.py, interfaces.py)
- When verifying that a refactoring has not introduced convention violations

## Inputs

| Parameter       | Type            | Required | Description                                                        |
|-----------------|-----------------|----------|--------------------------------------------------------------------|
| code            | string          | Yes      | Code to check against Vetinari best practices                      |
| mode            | string          | No       | "code_review" with best practices focus                            |
| context         | dict            | No       | File path, module type, recent changes                             |
| focus_areas     | list[string]    | No       | VET rule categories: "imports", "naming", "completeness", "docs"   |
| thinking_mode   | string          | No       | Thinking budget tier                                               |

## Process Steps

1. **Import convention check (VET010-019)** -- Verify import patterns:
   - `from __future__ import annotations` is the first import in every `vetinari/` file
   - Enums imported from `vetinari.types` (not redefined locally)
   - Agent specs imported from `vetinari.agents.contracts` (not redefined)
   - No wildcard imports (`from module import *`)
   - Import order: stdlib, third-party, local
   - No circular imports introduced

2. **Naming convention check (VET020-029)** -- Verify naming patterns:
   - Functions and variables: `snake_case`
   - Classes: `PascalCase`
   - Constants: `UPPER_SNAKE_CASE`
   - Boolean variables: `is_`, `has_`, `can_`, `should_` prefixes
   - Test files: `test_<module>.py`
   - Test functions: `test_<function>_<scenario>`
   - No single-letter names except in comprehensions

3. **Completeness check (VET030-039)** -- Verify no stubs or placeholders:
   - No `TODO`, `FIXME`, `HACK`, `XXX` without issue reference
   - No `pass` as function body (except abstract methods)
   - No `...` as function body (except type stubs)
   - No `raise NotImplementedError` (except in abstract methods)
   - No placeholder strings: "placeholder", "example", "test data", "foo", "bar"
   - No commented-out code blocks
   - No empty function bodies (only docstring, no implementation)

4. **Logging convention check (VET035, VET050-051)** -- Verify logging patterns:
   - `import logging` and `logger = logging.getLogger(__name__)` present
   - No `print()` in production code (allowed in `__main__`, `cli`, `scripts/`, `tests/`)
   - Logger uses %-style formatting: `logger.info("x=%s", x)` not `logger.info(f"x={x}")`
   - `logger.exception()` used in except blocks (includes traceback)
   - No `logging.info()` (root logger) -- use `logger.info()` (module logger)

5. **Type annotation check (VET040-049)** -- Verify type safety:
   - All function signatures fully annotated (parameters and return)
   - Modern syntax: `list[str]` not `List[str]`, `X | None` not `Optional[X]`
   - No `Any` without justification
   - Dataclass or Pydantic model for structured data (not raw dict)
   - Enum for fixed value sets (not string literals)

6. **Robustness check (VET060-069)** -- Verify defensive coding:
   - `encoding="utf-8"` on all `open()` calls
   - `pathlib.Path` for path handling (not `os.path.join`)
   - No `breakpoint()`, `import pdb`, or `pdb.set_trace()`
   - No `time.sleep()` > 5 seconds in production code
   - Context managers for resource management
   - Specific exception types (not bare `except:`)

7. **Dependency check (VET070-079)** -- Verify dependency hygiene:
   - Every third-party import has a pyproject.toml entry
   - No pinned versions without justification
   - No removed dependencies that are still imported

8. **Documentation check (VET090-102)** -- Verify documentation quality:
   - Every public function has a Google-style docstring
   - Docstrings are meaningful (>10 characters, not restating the name)
   - Functions with 2+ params have Args section
   - Functions with returns have Returns section
   - Module-level docstring present
   - Markdown files have title heading, no empty sections

9. **Wiring check (VET080-089)** -- Verify integration completeness:
   - Every new function is called from at least one place
   - Every new class is instantiated or referenced
   - Every new module is imported in `__init__.py`
   - No orphaned code (defined but unreachable)

10. **Finding compilation** -- Map each violation to its VET rule ID with severity and remediation guidance.

## Output Format

The skill produces a best practices report:

```json
{
  "passed": false,
  "grade": "C",
  "score": 0.68,
  "issues": [
    {
      "severity": "high",
      "category": "imports",
      "description": "VET010: Missing 'from __future__ import annotations' as first import",
      "file": "vetinari/orchestration/stagnation.py",
      "line": 1,
      "suggestion": "Add 'from __future__ import annotations' as the first line after the module docstring"
    },
    {
      "severity": "high",
      "category": "completeness",
      "description": "VET030: TODO comment without issue reference on line 45",
      "file": "vetinari/orchestration/stagnation.py",
      "line": 45,
      "suggestion": "Either implement the TODO or add an issue reference: TODO(#123)"
    },
    {
      "severity": "medium",
      "category": "logging",
      "description": "VET051: Using f-string in logger call instead of %-style formatting",
      "file": "vetinari/orchestration/stagnation.py",
      "line": 32,
      "suggestion": "Change logger.info(f'Processing {task}') to logger.info('Processing %s', task)"
    },
    {
      "severity": "medium",
      "category": "documentation",
      "description": "VET090: Public function detect_stagnation() missing docstring",
      "file": "vetinari/orchestration/stagnation.py",
      "line": 20,
      "suggestion": "Add Google-style docstring with Args and Returns sections"
    }
  ],
  "metrics": {
    "rules_checked": 31,
    "violations": 4,
    "violations_by_category": {
      "imports": 1,
      "completeness": 1,
      "logging": 1,
      "documentation": 1
    },
    "compliance_rate": 0.87
  },
  "suggestions": [
    "Run 'python scripts/check_vetinari_rules.py vetinari/orchestration/stagnation.py' to get automated VET rule output"
  ]
}
```

## Quality Standards

This skill is governed by the following standards from the skill registry:

- **STD-INS-001**: Code review MUST check all 5 dimensions (style includes project-specific conventions)
- **STD-INS-005**: Every issue MUST have a severity level and actionable description
- **STD-INS-006**: Inspector MUST NOT modify code -- only report findings and suggestions
- **STD-INS-007**: Gate decision MUST be based on objective criteria
- **STD-INS-008**: Gate decisions cannot be overridden by any other agent
- **STD-UNI-001**: All skill outputs MUST conform to the declared output_schema
- **CON-INS-001**: Inspector is READ-ONLY -- cannot modify production files

## Examples

### Example: Checking a new module for Vetinari conventions

**Input:**
```
code: |
  from typing import List, Optional
  import logging

  def processTask(task, config):
      print(f"Processing {task}")
      result = {}
      try:
          result = do_work(task)
      except:
          pass
      return result
context: {file: "vetinari/planning/task_processor.py"}
```

**Output (abbreviated):**
```
passed: false
grade: F
violations:
  - [critical/VET010] "Missing 'from __future__ import annotations'"
  - [critical/VET012] "Using typing.List and typing.Optional -- use list[] and X | None"
  - [high/VET020] "Function name processTask uses camelCase -- must be snake_case: process_task"
  - [high/VET035] "print() in production code -- use logger.info()"
  - [high/VET051] "f-string in print/log call -- use %-style: logger.info('Processing %s', task)"
  - [high/VET040] "Missing type annotations on function parameters and return type"
  - [high/VET060] "Bare except clause -- catch specific exception types"
  - [high/VET061] "Silent exception swallowing (except: pass) -- log or re-raise"
  - [high/VET090] "Missing docstring on public function"
  - [medium/VET091] "Missing module-level docstring"

compliance_rate: 0.10 (1 of 10 checked rules pass)
recommendation: "Significant convention violations. This code needs rewriting to match Vetinari conventions before it can pass the quality gate."
```
