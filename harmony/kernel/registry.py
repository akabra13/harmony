"""A tiny name-keyed plugin registry.

Detectors, providers, tools and gate rules are all "things the company registers
that the kernel then looks up by name". Rather than four bespoke registries with
four subtly different duplicate-name behaviours, they share this one.

Registration happens as an import side effect of the decorators in each subsystem,
so ``northfield/__init__.py`` importing its plugin modules is what populates every
catalog. :func:`load_plugin_modules` is the single place that import happens.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from typing import Generic, TypeVar

from harmony.kernel.errors import DuplicateRegistration, NotRegistered

T = TypeVar("T")


class Registry(Generic[T]):
    """Name → entry, with a helpful error when a name is missing."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._entries: dict[str, T] = {}

    def register(self, name: str, entry: T, *, replace: bool = False) -> T:
        if name in self._entries and not replace:
            raise DuplicateRegistration(
                f"{self.kind} '{name}' is already registered", kind=self.kind, name=name
            )
        self._entries[name] = entry
        return entry

    def get(self, name: str) -> T:
        try:
            return self._entries[name]
        except KeyError:
            raise NotRegistered(
                f"no {self.kind} named '{name}' is registered",
                kind=self.kind,
                name=name,
                available=sorted(self._entries),
            ) from None

    def try_get(self, name: str) -> T | None:
        return self._entries.get(name)

    def names(self) -> list[str]:
        return sorted(self._entries)

    def all(self) -> list[T]:
        return [self._entries[name] for name in sorted(self._entries)]

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[tuple[str, T]]:
        return iter(sorted(self._entries.items()))

    def clear(self) -> None:
        """Used by tests that register throwaway plugins."""
        self._entries.clear()


def load_plugin_modules(package: str) -> list[str]:
    """Import every module under ``package`` so its decorators run.

    Returns the module names imported, which the CLI prints under ``--verbose`` so
    that "my tool isn't registered" is a one-command diagnosis.
    """
    imported: list[str] = []
    root = importlib.import_module(package)
    for _finder, name, _ispkg in pkgutil.walk_packages(root.__path__, prefix=f"{package}."):
        importlib.import_module(name)
        imported.append(name)
    return imported
