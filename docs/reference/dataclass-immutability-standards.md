# Dataclass Immutability Standards

This standard covers FSA-00260. Production dataclasses in `vetinari/` should be
immutable value records by default:

- Use `@dataclass(frozen=True, slots=True)` for request, response, result,
  config, event, evidence, and other value-record classes.
- Do not leave a frozen dataclass unslotted unless a compatibility reason is
  documented in code and enforced by the checker.
- Keep mutable dataclasses only when the class is runtime state rather than a
  value record. Mechanically defensible mutable cases include explicit
  self-attribute mutation, mutable container fields, mutator methods,
  post-init normalization, state field names such as `tokens` or `updated_at`,
  and role names such as `State`, `Store`, `Registry`, `Manager`, `Bucket`, or
  `Resources`.

`scripts/check_dataclass_immutability.py` is the enforcement point. It fails
when an unslotted or mutable dataclass cannot be classified as intentional
runtime state. The checker is not a blanket baseline: value records remain
actionable until converted to `frozen=True, slots=True`.

Run:

```powershell
python scripts/check_dataclass_immutability.py --json
```
