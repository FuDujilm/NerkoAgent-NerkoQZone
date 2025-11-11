"""Compatibility helpers that expose runtime objects to legacy modules."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple, Type


class _ComponentRegistry:
    """Minimal registry used by the legacy MaiZone modules.

    The original project accessed the running plugin through a global registry
    named ``component_registry``.  The Nerko plugin SDK does not expose such a
    helper, therefore we provide a tiny shim that can be populated at runtime
    by :mod:`plugin.py` once the :class:`~nekro_agent.api.plugin.NekroPlugin`
    instance is created.
    """

    def __init__(self) -> None:
        self._plugin_factories: Dict[str, Tuple[Callable[[], Any], Type[Any]]] = {}

    # -- registration -------------------------------------------------
    def register_plugin(
        self,
        name: str,
        plugin_factory: Callable[[], Any],
        config_model: Type[Any],
    ) -> None:
        """Register a plugin accessor.

        ``plugin_factory`` is a callable that returns the live plugin instance
        when invoked.  This indirection keeps import cycles at bay because the
        resolver can be defined with a ``lambda`` that performs the import on
        demand.
        """

        self._plugin_factories[name] = (plugin_factory, config_model)

    # -- access helpers ----------------------------------------------
    def get_plugin_config(self, name: str) -> Optional[Any]:
        factory = self._plugin_factories.get(name)
        if not factory:
            return None

        plugin_factory, config_model = factory
        try:
            plugin = plugin_factory()
            if plugin is None:
                return None
            return plugin.get_config(config_model)
        except Exception:
            return None

    def get_plugin(self, name: str) -> Optional[Any]:
        factory = self._plugin_factories.get(name)
        if not factory:
            return None

        plugin_factory, _ = factory
        try:
            return plugin_factory()
        except Exception:
            return None


component_registry = _ComponentRegistry()

