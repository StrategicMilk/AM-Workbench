# AM Workbench Skill Registry

## Overview

The Skill Registry is the Python-backed discovery and validation surface for AM
Workbench skills. It lets agents discover available skills, capabilities,
permissions, and sample usage from the live catalog instead of from retired JSON
registry files.

## Architecture

### Components

1. **Runtime Registry API** (`vetinari/skills/skill_registry.py`)
   - Public entrypoint for discovering skills from Python
   - Delegates storage, catalog loading, and governance checks to the
     `vetinari/skills/skill_registry_*` modules
   - Returns validated skill specs from the live catalog

2. **Catalog Skill Definitions** (`vetinari/skills/catalog/**/SKILL.md`)
   - Source-of-truth skill instructions grouped by Foreman, Worker, and Inspector
   - Parsed by `vetinari/skills/catalog_loader.py`
   - Reflected into registry data through `vetinari/skills/skill_definitions.py`

3. **Registry Class and Governance** (`vetinari/skills/skill_registry_class.py`,
   `vetinari/skills/skill_registry_governance.py`)
   - Enforces schema, permissions, routing metadata, and compatibility checks
   - Keeps registry behavior in code instead of a stale JSON index

4. **Context Asset Registry** (`vetinari/workbench/context_assets/registry.py`)
   - Registers workbench context packs and freshness metadata
   - Replaces the retired JSON context-registry documentation path

## Usage

### Python API

```python
from vetinari.skills.skill_registry import get_registry

registry = get_registry()

# List all skills
skills = registry.list_skills()
print(f"Available skills: {[s['id'] for s in skills]}")

# Get specific skill
skill = registry.get_skill("worker")
print(f"Worker capabilities: {skill['capabilities']}")

# Get skill manifest
manifest = registry.get_skill_manifest("inspector")
print(f"Required permissions: {manifest['permissions_required']}")

# Get skills for an agent
agent_skills = registry.get_agent_skills("worker")
print(f"Worker agent skills: {agent_skills}")

# Search skills
results = registry.search_skills("review")
print(f"Matching skills: {[s['id'] for s in results]}")

# Validate registry
validation = registry.validate()
print(f"Errors: {validation['errors']}")
print(f"Warnings: {validation['warnings']}")
```

### CLI Usage

There is no dedicated registry CLI module. Use the Python API above or the
project test suite for registry validation. The retired Litestar web host is not
the primary runtime API boundary; document a Rust kernel route only after the
route and validation job exist.

## Adding a New Skill

1. Add or update the skill definition under `vetinari/skills/catalog/<role>/<skill-id>/SKILL.md`.
2. Update `vetinari/skills/skill_definitions.py` only when the skill needs new
   structured metadata or defaults beyond the catalog file.
3. Update registry loaders or governance modules only when the new skill changes
   parsing, permissions, routing metadata, or validation behavior.
4. Add workbench context packs through `vetinari/workbench/context_assets/registry.py`
   when the skill depends on reusable context assets.
5. Run validation through `get_registry().validate()` or a focused registry test;
   do not document a CI or CLI validation path unless the job or command exists.

## Manifest Schema

```json
{
  "skill_id": "string",
  "name": "string",
  "version": "semver",
  "description": "string",
  "capabilities": ["string"],
  "thinking_modes": ["low", "medium", "high", "xhigh"],
  "triggers": ["string"],
  "required_permissions": ["FILE_READ"],
  "allowed_modes": ["EXECUTION", "PLANNING"],
  "sample_usage": {},
  "inputs": {},
  "outputs": {},
  "contexts": ["context_id"],
  "external_endpoints": {
    "allowed": false,
    "endpoints": ["url"]
  }
}
```

## Workflows

Predefined skill workflows follow the 3-agent factory pipeline (ADR-0061):

- `code_review_pipeline`: Worker(code_discovery) -> Inspector(code_review) -> Worker(documentation)
- `feature_implementation_pipeline`: Worker(code_discovery) -> Worker(architecture) -> Worker(build) -> Inspector(code_review) -> Worker(documentation)
- `research_pipeline`: Worker(domain_research) -> Worker(lateral_thinking) -> Worker(architecture) -> Worker(synthesis)

### Skill Catalog Structure

Skills are organized by agent type in `vetinari/skills/catalog/`:

- `foreman/` - Planning and decomposition skills
- `worker/` - Skills across research, architecture, build, and operations groups
- `inspector/` - Review, audit, testing, and simplification skills

Each skill has a `SKILL.md` definition file describing its purpose, inputs,
outputs, and quality criteria.

## Version Compatibility

Registry compatibility is enforced by the live registry API and tests. Do not
add a static compatibility matrix to this document unless a runtime reader and
validation test consume it.

## Security

- All skills enforce permissions via `ToolMetadata.required_permissions`.
- External network access is explicitly whitelisted per skill.
- Registry validation must be run through the live registry API or tests. Do not
  claim CI coverage unless the workflow job and checked roots are named.
- Skill proposal and validation routes are internal control-plane surfaces.
  Treat pending proposals as untrusted until reviewed; capability entries must
  be schema-validated strings before they are allowed to influence routing or
  trust decisions.
