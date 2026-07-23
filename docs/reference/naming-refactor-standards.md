# Naming And Refactor Opportunity Standards

This standard turns refactor-opportunity inventory counts into
branch-discriminating checks. It covers the closure criteria for FSA-00256,
FSA-00257, FSA-00264, and FSA-00267.

## Verb Families

`build_*` and `create_*` may coexist when they describe different operations:

- `build_*` assembles, derives, formats, or returns an in-memory artifact.
- `create_*` allocates, persists, registers, starts, or otherwise introduces a
  new runtime or domain resource.

`set_*` and `update_*` may coexist when they describe different mutation shapes:

- `set_*` replaces one explicit field, callback, context value, mode, or policy.
- `update_*` merges a partial payload, records an observation, or changes an
  existing aggregate according to domain rules.

The closure check is therefore same-scope and same-suffix ambiguity, not raw
verb totals. `scripts/check_refactor_opportunity_verbs.py` fails when a module
or class exposes both verbs for the same suffix, such as `build_plan()` and
`create_plan()` in one scope.

## Method Shape

A method body that does not reference `self` is an inventory signal, not by
itself a safe conversion. Keep instance-method shape when the method is part of
public API compatibility, a protocol or override contract, a lifecycle hook, or
a monkeypatch/test compatibility surface. Use `@staticmethod` only when the
method is a private implementation helper and changing bound-method
introspection is acceptable for that class.

`scripts/check_refactor_opportunity_methods.py` reports both the raw zero-self
inventory and the actionable slice. A zero-self method is excluded from the
actionable slice only when the script can prove one of these compatibility
cases from syntax and project structure:

- public method name, because bound-method shape is part of the public API;
- data-model or lifecycle dunder, such as `__repr__` or `__enter__`;
- class inheriting from `Protocol`, `ABC`, or `ABCMeta`;
- explicit `@override` or a method with the same name on a known project base
  class;
- repeated private hook name that is dispatched through `self.<name>()`,
  indicating a polymorphic template-method surface.

All other private zero-self instance methods are actionable staticmethod
candidates. The check fails while any actionable zero-self candidates remain;
the raw inventory can still be used for trending and bounded stricter runs.

`_ensure_*` is a cached-property candidate only when it is synchronous, takes no
arguments beyond `self`, assigns and returns the same `self` attribute, and no
other method resets or recreates that attribute. Resettable lifecycle helpers
must stay methods. `scripts/check_refactor_opportunity_methods.py` fails on
actionable cached-property candidates and can fail the full raw zero-self
inventory for a bounded stricter slice via `--fail-zero-self`.
