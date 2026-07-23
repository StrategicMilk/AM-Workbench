"""Base Protocol for mixin attribute-dependency contracts.

Every ``*Mixin`` class in vetinari that accesses sibling-class attributes must
declare a companion Protocol listing those attributes. The mixin class body
declares each required attribute as ``if TYPE_CHECKING: attr: Type`` stubs so
pyright can validate cross-mixin attribute access in isolation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MixinProtocol(Protocol):
    """Marker base for all per-mixin attribute dependency Protocols."""

    ...


def assert_protocol_satisfied(host: object, protocol: type) -> None:
    """Raise TypeError if ``host`` does not structurally satisfy ``protocol``.

    Args:
        host: The composed host instance (e.g. AutonomyGovernor) expected to
            carry every attribute its mixin Protocol declares.
        protocol: A runtime_checkable Protocol subtype of MixinProtocol.

    Raises:
        TypeError: When ``isinstance(host, protocol)`` is False, with a message
            naming the host type and the unsatisfied protocol.
    """
    if not isinstance(host, protocol):
        raise TypeError(
            f"{type(host).__name__} does not satisfy the {protocol.__name__} "
            f"mixin contract: one or more required attributes/methods are absent."
        )


__all__ = ["MixinProtocol", "assert_protocol_satisfied"]
