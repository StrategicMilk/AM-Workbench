---
name: Ontological Mapping
description: Map domain concepts to code types ensuring domain model fidelity between business entities and code structures
mode: ontological_analysis
agent: worker
version: "1.0.0"
capabilities:
  - ontological_mapping
  - architecture_review
tags:
  - architecture
  - domain-model
  - ontology
  - mapping
---

# Ontological Mapping

## Purpose

Ontological Mapping establishes a formal correspondence between domain concepts (business entities, processes, relationships) and their code representations (classes, enums, interfaces, data flows). It ensures that the code accurately models the domain -- that every domain concept has a code representation, every code structure maps to a domain concept, and the relationships between concepts are preserved in code. This prevents the "impedance mismatch" where code drifts from the domain model, making the system harder to understand and evolve.

## When to Use

- When designing a new subsystem that models a real-world domain
- When domain experts describe requirements using terms that do not map cleanly to existing code
- When the codebase has accumulated technical jargon that diverges from domain language
- When onboarding new contributors who need to understand the domain-to-code mapping
- When refactoring to improve domain model fidelity (aligning code names with domain terms)
- When the Vetinari factory metaphor needs to be validated against actual code structures

## Inputs

| Parameter       | Type            | Required | Description                                                        |
|-----------------|-----------------|----------|--------------------------------------------------------------------|
| task            | string          | Yes      | What domain to map and the mapping objective                       |
| domain_concepts | list[dict]      | No       | Domain entities with their properties and relationships            |
| code_files      | list[string]    | No       | Source files containing the code model to map against               |
| glossary        | dict            | No       | Domain glossary (term -> definition)                               |
| context         | dict            | No       | Project context, existing mappings, known mismatches               |

## Process Steps

1. **Domain model extraction** -- Identify all domain concepts from requirements, documentation, and stakeholder language. For each concept, record: name, definition, properties, relationships to other concepts, and lifecycle (created, modified, archived).

2. **Code model extraction** -- Scan the codebase for classes, enums, dataclasses, and TypedDicts that represent domain entities. Map their fields, methods, inheritance hierarchies, and module locations.

3. **Correspondence mapping** -- Create a mapping table between domain concepts and code structures. For each pairing, note:
   - **Direct**: domain concept has a 1:1 code representation with matching name
   - **Aliased**: domain concept exists in code but under a different name
   - **Distributed**: domain concept is spread across multiple code structures
   - **Absent**: domain concept has no code representation
   - **Orphaned**: code structure has no corresponding domain concept

4. **Relationship preservation check** -- Verify that relationships between domain concepts are preserved in code:
   - Domain "has-a" should map to composition or field reference
   - Domain "is-a" should map to inheritance or enum membership
   - Domain "depends-on" should map to import or parameter dependency
   - Domain processes should map to functions or methods

5. **Naming alignment assessment** -- Compare domain terminology with code naming. Flag cases where the code uses a different term for the same concept (e.g., domain says "approval" but code says "validation"). Ubiquitous Language from Domain-Driven Design demands consistent terminology.

6. **Invariant identification** -- Identify domain invariants (rules that must always be true) and check if they are enforced in code. For example, "a plan always has at least one task" should be enforced by the Plan constructor or validator.

7. **Boundary context mapping** -- Identify bounded contexts in the domain and verify they correspond to module boundaries in code. Concepts that belong to different bounded contexts should be in different modules.

8. **Gap analysis** -- For each absent domain concept and orphaned code structure, determine if this is: intentional (concept not needed in code, or code is infrastructure), a deficiency (concept should be modeled), or debt (planned but not yet implemented).

9. **Recommendation synthesis** -- Produce a mapping report with: complete correspondence table, alignment score, gaps, and recommended actions (rename, extract, consolidate, remove).

## Output Format

The skill produces an ontological mapping report:

```json
{
  "success": true,
  "output": {
    "alignment_score": 0.78,
    "correspondence_table": [
      {
        "domain_concept": "Foreman",
        "definition": "Plans work, decomposes goals, assigns tasks",
        "code_representation": "vetinari/agents/planner_agent.py:ForemanAgent",
        "mapping_type": "direct",
        "naming_aligned": true
      },
      {
        "domain_concept": "Work Order",
        "definition": "A complete specification of work to be done",
        "code_representation": "vetinari/agents/contracts.py:Plan",
        "mapping_type": "aliased",
        "naming_aligned": false,
        "note": "Domain calls it 'Work Order', code calls it 'Plan'"
      },
      {
        "domain_concept": "Quality Gate",
        "definition": "Checkpoint where work must pass inspection before proceeding",
        "code_representation": "No explicit code structure",
        "mapping_type": "absent",
        "note": "Implicit in Inspector agent behavior but not modeled as a first-class entity"
      }
    ],
    "invariants": [
      {
        "domain_rule": "Every task must be assigned to exactly one agent",
        "enforced_in_code": true,
        "enforcement": "Task.assigned_agent is required field (non-nullable)"
      }
    ],
    "gaps": [
      {"type": "absent", "concept": "Quality Gate", "recommendation": "Model as dataclass with pass/fail state"},
      {"type": "orphaned", "code": "ExecutionContext", "recommendation": "Document as infrastructure (no domain concept)"}
    ]
  },
  "metadata": {
    "domain": "manufacturing-inspired agent orchestration",
    "adr_reference": "ADR-0061 (factory pipeline architecture)"
  }
}
```

## Quality Standards

This skill is governed by the following standards from the skill registry:

- **STD-WRK-004**: Architecture modes are READ-ONLY -- MUST NOT modify production files
- **STD-WRK-005**: Every design MUST state the chosen pattern and its rationale
- **STD-WRK-006**: Component designs MUST define clear boundaries (inputs, outputs, dependencies)
- **STD-UNI-001**: All skill outputs MUST conform to the declared output_schema
- **CON-WRK-002**: Architecture modes are READ-ONLY -- produce ADRs, not code changes

## Examples

### Example: Validating the factory metaphor

**Input:**
```
task: "Map the manufacturing factory metaphor to Vetinari's actual code structures"
domain_concepts: [
  {name: "Factory Floor", definition: "The overall system where work happens"},
  {name: "Foreman", definition: "Supervises and plans work"},
  {name: "Worker", definition: "Executes tasks"},
  {name: "Inspector", definition: "Verifies quality"},
  {name: "Work Order", definition: "Specification of work to be done"},
  {name: "Assembly Line", definition: "Sequential processing pipeline"}
]
```

**Output (abbreviated):**
```
alignment_score: 0.83

correspondence:
  "Factory Floor" -> "vetinari/orchestration/" (direct, well-aligned)
  "Foreman" -> "AgentType.FOREMAN" + "ForemanAgent" (direct, well-aligned)
  "Worker" -> "AgentType.WORKER" + "WorkerAgent" (direct, well-aligned)
  "Inspector" -> "AgentType.INSPECTOR" + "InspectorAgent" (direct, well-aligned)
  "Work Order" -> "Plan" dataclass (aliased -- domain says "work order", code says "plan")
  "Assembly Line" -> "two_layer.py" orchestration (distributed across multiple modules)

gaps:
  - "Work Order" aliased as "Plan" -- consider renaming for ubiquitous language
  - "Assembly Line" concept is implicit in orchestration code, not explicitly modeled
  - "Quality Gate" (manufacturing concept) not modeled as first-class entity
```
