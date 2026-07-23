"""Singleton helper utilities."""

from __future__ import annotations

import threading
from typing import Any, TypeVar, cast

T = TypeVar("T")


class ThreadSafeSingleton:
    """Thread-safe singleton base class."""

    _instance: object | None = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls: type[T]) -> T:
        """Return the singleton instance for this class.

        Returns:
            The singleton instance.
        """
        typed_cls = cast(Any, cls)
        instance = typed_cls._instance
        if instance is None:
            lock = cast(threading.Lock, typed_cls._lock)
            with lock:
                instance = typed_cls._instance
                if instance is None:
                    instance = cls()
                    typed_cls._instance = instance
        return cast(T, instance)

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton instance."""
        with cls._lock:
            cls._instance = None


def parameterized_singleton(cls: type[T]) -> type[T]:
    """Decorate a class so identical constructor args share one instance.

    Args:
        cls: Class to wrap.

    Returns:
        Wrapped singleton class.
    """
    instances: dict[tuple[tuple[Any, ...], tuple[tuple[str, Any], ...]], T] = {}
    first_key: tuple[tuple[Any, ...], tuple[tuple[str, Any], ...]] | None = None
    lock = threading.RLock()

    def __new__(inner_cls: type[T], *args: Any, **kwargs: Any) -> T:
        nonlocal first_key
        key = (args, tuple(sorted(kwargs.items())))
        with lock:
            if first_key is not None and key != first_key:
                raise ValueError("singleton constructor arguments changed")
            first_key = key
            if key not in instances:
                instances[key] = _allocate_parameterized_singleton(inner_cls)
            return instances[key]

    def reset_all(inner_cls: type[T]) -> None:
        """Clear singleton instances."""
        nonlocal first_key
        with lock:
            instances.clear()
            first_key = None

    return cast(
        type[T],
        type(
            cls.__name__,
            (cls,),
            {
                "__new__": __new__,
                "reset_all": classmethod(reset_all),
                "__doc__": "Parameterized singleton wrapper.",
            },
        ),
    )


def _allocate_parameterized_singleton(inner_cls: type[T]) -> T:
    """Allocate a singleton instance while the caller holds the singleton lock."""
    return cast(T, object.__new__(inner_cls))


def thread_safe_singleton(cls: type[T]) -> type[T]:
    """Decorate a class as a thread-safe singleton.

    Args:
        cls: Class to wrap.

    Returns:
        Wrapped singleton class.
    """
    return parameterized_singleton(cls)


__all__ = ["ThreadSafeSingleton", "parameterized_singleton", "thread_safe_singleton"]
