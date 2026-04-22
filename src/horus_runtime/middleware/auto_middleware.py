#
# horus-runtime
# Copyright (C) 2026 Temple Compute
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""
Automatic middleware registry system for the horus-runtime.
"""

from abc import ABC
from collections.abc import Awaitable, Callable
from importlib.metadata import entry_points
from inspect import isabstract
from typing import Any, ClassVar, Self, TypeVar

from horus_runtime.logging import horus_logger

HORUS_MIDDLEWARE_ENTRY_POINT_PREFIX = "horus.middleware."

# Return type of the call_next function passed to middleware.wrap()
R = TypeVar("R")


class AutoMiddleware[T = Any](ABC):
    """
    Middleware registry for the horus-runtime.
    """

    _registry_roots: ClassVar[dict[type["AutoMiddleware"], str]] = {}
    registry: list[type[Self]]

    def __init_subclass__(
        cls,
        entry_point: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Mimic the standard AutoRegistry. Load middleware from entry-points.
        """
        super().__init_subclass__(**kwargs)

        if entry_point:
            prefixed = HORUS_MIDDLEWARE_ENTRY_POINT_PREFIX + entry_point
            AutoMiddleware._registry_roots[cls] = prefixed
            cls.registry = []
            return

        # Concrete subclass — find its domain root and register there
        if isabstract(cls):
            return

        for root in AutoMiddleware._registry_roots:
            if issubclass(cls, root):
                root.registry.append(cls)
                return

        raise TypeError(
            f"{cls.__name__} inherits from AutoMiddleware but no domain root "
            f"(entry_point=...) was found in its MRO."
        )

    @staticmethod
    def init_registry() -> None:
        """
        Call once at boot. Loads all horus.middleware.* entry point groups.
        Importing the module triggers __init_subclass__ and populates
        registries.
        """
        groups_to_load = {
            group
            for group in entry_points().groups
            if group.startswith(HORUS_MIDDLEWARE_ENTRY_POINT_PREFIX)
        }
        for group in groups_to_load:
            for middleware_plugin in entry_points(group=group):
                try:
                    middleware_plugin.load()
                except Exception as e:
                    horus_logger.log.error(f"Error loading middleware: {e}")

    async def before(self, context: T) -> None:
        """
        Hook called before execution.
        """
        del context

    async def after(self, context: T) -> None:
        """
        Hook called after execution.
        """
        del context

    async def wrap(
        self,
        context: T,
        call_next: Callable[[], Awaitable[R]],
    ) -> R:
        """
        Wrap execution of the next middleware or the target callable.

        The default implementation preserves ``before``/``after`` semantics,
        while allowing middleware to override ``wrap`` directly for richer
        behaviors such as timeouts, retries, or exception translation.
        """
        await self.before(context)
        try:
            return await call_next()
        finally:
            await self.after(context)

    @classmethod
    async def call_with_middleware(
        cls,
        context: T,
        call_next: Callable[[], Awaitable[R]],
    ) -> R:
        """
        Execute *call_next* through the registered middleware chain.
        """
        middlewares = [m() for m in cls.registry]  # instantiate per-context

        async def invoke(index: int) -> R:
            if index >= len(middlewares):
                return await call_next()

            middleware = middlewares[index]
            return await middleware.wrap(
                context,
                lambda: invoke(index + 1),
            )

        return await invoke(0)
