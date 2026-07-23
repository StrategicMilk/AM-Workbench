"""Grammar-constrained decoding helpers."""

from __future__ import annotations

from dataclasses import replace

from vetinari.inference.cpu_tier import CpuTierInterface
from vetinari.inference.request import RoutedInferenceRequest
from vetinari.inference.result import InferenceResult

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
CONSTRAINED_INFERENCE_WORKFLOW_GUARDS: tuple[str, ...] = (
    "enum grammars reject empty allowed-value lists",
    "schema grammars reject non-object JSON schema roots",
    "constrained requests cap completions at 64 tokens",
    "out-of-grammar completions raise instead of returning unsafe output",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return constrained-inference workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/inference/constrained.py",
        "guards": CONSTRAINED_INFERENCE_WORKFLOW_GUARDS,
    }


def build_enum_grammar(values: list[str]) -> str:
    """Build a GBNF grammar that permits only the provided enum values.

    Returns:
        Newly constructed enum grammar value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not values:
        raise ValueError("allowed enum values must not be empty")
    escaped = [value.replace("\\", "\\\\").replace('"', '\\"') for value in values]
    alternatives = " | ".join(f'"{value}"' for value in escaped)
    return f"root ::= {alternatives}\n"


def build_json_schema_grammar(schema: dict) -> str:
    """Build a GBNF grammar that constrains output to a JSON-schema object (FSA-0055).

    The grammar lets the model emit a JSON object whose keys are exactly the
    schema's ``properties`` entries.  Property values are emitted as JSON
    strings, numbers, booleans, ``null``, or nested objects/arrays — the
    grammar deliberately keeps value typing permissive so the model is not
    blocked by overly tight per-property constraints; downstream schema
    validation can reject mistyped values without requiring a grammar that
    encodes every type in the GBNF.

    Args:
        schema: A JSON-schema fragment.  Must have ``type == "object"``;
            other top-level types (array, string, number, ...) raise
            ``ValueError`` so the caller selects the right grammar builder
            rather than getting silently coerced.

    Returns:
        A GBNF grammar string whose root production is a JSON object with
        the requested keys.

    Raises:
        ValueError: If ``schema['type']`` is anything other than ``"object"``.
    """
    schema_type = schema.get("type")
    if schema_type != "object":
        raise ValueError(f"Only object schemas are supported by build_json_schema_grammar; got type={schema_type!r}")
    properties = schema.get("properties") or {}
    property_names = list(properties)
    if not property_names:
        # An empty object {} is valid JSON-schema-wise — generate a grammar
        # that accepts only an empty object literal.
        return 'root ::= "{" ws "}"\nws ::= ([ \\t\\n])*\n'

    # Build per-property productions that pair the literal key with a
    # permissive value production.  The keys are interleaved with commas
    # rather than being made order-free; downstream JSON tooling normalises
    # key ordering, and a position-free grammar would be massively larger.
    key_productions: list[str] = []
    for name in property_names:
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        key_productions.append(f'"\\"{escaped}\\"" ws ":" ws value')

    body = ' ws "," ws '.join(key_productions)
    grammar_lines = [
        'root ::= "{" ws ' + body + ' ws "}"',
        'value ::= string | number | object | array | "true" | "false" | "null"',
        'object ::= "{" ws ( string ws ":" ws value ( ws "," ws string ws ":" ws value )* )? ws "}"',
        'array ::= "[" ws ( value ( ws "," ws value )* )? ws "]"',
        'string ::= "\\"" ([^"\\\\] | "\\\\" .)* "\\""',
        'number ::= "-"? ([0-9]+) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?',
        "ws ::= ([ \\t\\n])*",
    ]
    return "\n".join(grammar_lines) + "\n"


class ConstrainedDecoder:
    """Wrapper that validates constrained enum completions."""

    def __init__(self, cpu_tier: CpuTierInterface) -> None:
        self._cpu_tier = cpu_tier

    def complete(self, request: RoutedInferenceRequest, allowed_values: list[str]) -> InferenceResult:
        """Run a constrained completion and reject out-of-grammar output.

        Args:
            request: Request object sent through the operation.
            allowed_values: Value processed by the operation.

        Returns:
            InferenceResult value produced by complete().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        grammar = build_enum_grammar(allowed_values)
        constrained_request = replace(request, max_tokens=min(request.max_tokens, 64), grammar=grammar)
        result = self._cpu_tier.complete(constrained_request)
        token = result.text.strip()
        if token not in allowed_values:
            raise ValueError(f"Constrained decode produced out-of-grammar token: {token}")
        return result


__all__ = ["ConstrainedDecoder", "build_enum_grammar", "developer_workflow_contract"]
