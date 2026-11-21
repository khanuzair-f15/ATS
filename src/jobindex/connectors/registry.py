"""Connector Registry.

Runtime registry mapping platform keys to connector factories.

Registration is explicit. The registry never walks the filesystem or imports a
package directory wholesale, so templates, helpers, auth modules, base classes
and tests can never become connector entries by accident.
"""

from __future__ import annotations

from collections.abc import Callable

from jobindex.connectors.base import Connector
from jobindex.connectors.context import ConnectorContext

ConnectorFactory = Callable[[ConnectorContext], Connector]
ConnectorDecorator = Callable[[ConnectorFactory], ConnectorFactory]


class ConnectorRegistry:
    """Runtime registry mapping platform keys to connector factories.

    The registry guarantees:
    - one factory per platform key, with duplicates rejected;
    - unknown keys fail loudly instead of returning ``None``;
    - factories are callable and produce a ``Connector``-compatible object;
    - ``list_platforms`` is deterministic.

    A platform key is an internal identifier (for example ``greenhouse``), not a
    display name. Case matters.
    """

    def __init__(self) -> None:
        self._factories: dict[str, ConnectorFactory] = {}

    def register(self, platform_key: str, factory: ConnectorFactory) -> None:
        """Register the factory for one platform.

        Raises:
            ValueError: if the key is empty or already registered.
            TypeError: if the factory is not callable.
        """

        if not isinstance(platform_key, str) or not platform_key.strip():
            raise ValueError("platform_key must be a non-empty string")
        if platform_key != platform_key.strip():
            raise ValueError("platform_key must not have leading or trailing whitespace")
        if platform_key in self._factories:
            raise ValueError(f"Connector for platform {platform_key!r} already registered")
        if not callable(factory):
            raise TypeError(f"Factory for {platform_key!r} must be callable")

        self._factories[platform_key] = factory

    def register_connector(self, platform_key: str) -> ConnectorDecorator:
        """Return a decorator that registers a connector factory."""

        def decorator(factory: ConnectorFactory) -> ConnectorFactory:
            self.register(platform_key, factory)
            return factory

        return decorator

    def get(self, platform_key: str, context: ConnectorContext) -> Connector:
        """Instantiate the registered connector for ``platform_key``.

        Raises:
            KeyError: if the platform is not registered.
            TypeError: if the factory result is not a ``Connector``.
            ValueError: if the connector reports a different platform key.
        """

        if platform_key not in self._factories:
            available = ", ".join(sorted(self._factories)) or "<none>"
            raise KeyError(
                f"No connector registered for platform {platform_key!r}. "
                f"Available: {available}"
            )

        connector = self._factories[platform_key](context)

        for attribute in ("platform_key", "transport", "completeness"):
            if not hasattr(connector, attribute):
                raise TypeError(
                    f"Connector for {platform_key!r} missing required attribute "
                    f"{attribute!r}"
                )
        for method in ("list_page", "fetch_detail"):
            if not callable(getattr(connector, method, None)):
                raise TypeError(
                    f"Connector for {platform_key!r} missing required method {method!r}"
                )
        if not isinstance(connector, Connector):
            raise TypeError(
                f"Factory for {platform_key!r} did not return a Connector"
            )
        if connector.platform_key != platform_key:
            raise ValueError(
                f"Connector for {platform_key!r} has mismatched platform_key: "
                f"{connector.platform_key!r}"
            )
        return connector

    def list_platforms(self) -> list[str]:
        """Return registered platform keys in deterministic order."""

        return sorted(self._factories)

    def __contains__(self, platform_key: object) -> bool:
        return isinstance(platform_key, str) and platform_key in self._factories

    def __len__(self) -> int:
        return len(self._factories)


# Global registry used by the runtime and the coverage command.
_registry = ConnectorRegistry()


def get_registry() -> ConnectorRegistry:
    """Return the process-wide registry."""

    return _registry


def register_connector(platform_key: str) -> ConnectorDecorator:
    """Register a connector factory on the process-wide registry."""

    return _registry.register_connector(platform_key)


def get_connector(platform_key: str, context: ConnectorContext) -> Connector:
    """Instantiate a connector from the process-wide registry."""

    return _registry.get(platform_key, context)


def list_connectors() -> list[str]:
    """List platform keys registered on the process-wide registry."""

    return _registry.list_platforms()


__all__ = [
    "ConnectorDecorator",
    "ConnectorFactory",
    "ConnectorRegistry",
    "get_connector",
    "get_registry",
    "list_connectors",
    "register_connector",
]
