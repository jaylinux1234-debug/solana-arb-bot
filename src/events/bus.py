"""Lightweight async event bus for decoupled monitoring and webhooks."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from src.events.types import BotEvent, EventKind

logger = logging.getLogger(__name__)

EventHandler = Callable[[BotEvent], Awaitable[None] | None]


class EventBus:
    """In-process pub/sub (single-process bot)."""

    def __init__(self) -> None:
        self._handlers: dict[EventKind, list[EventHandler]] = defaultdict(list)
        self._wildcard: list[EventHandler] = []

    def subscribe(
        self,
        kind: EventKind | None,
        handler: EventHandler,
    ) -> None:
        """Register ``handler`` for ``kind`` or all events when ``kind`` is None."""
        if kind is None:
            self._wildcard.append(handler)
        else:
            self._handlers[kind].append(handler)

    async def publish(self, event: BotEvent) -> None:
        """Dispatch ``event`` to subscribers (errors logged, never raised)."""
        handlers = list(self._wildcard) + list(self._handlers.get(event.kind, []))
        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning(
                    "Event handler failed | kind=%s handler=%s err=%s",
                    event.kind.value,
                    getattr(handler, "__name__", handler),
                    exc,
                    exc_info=True,
                )

    def publish_fire_and_forget(self, event: BotEvent) -> None:
        """Schedule :meth:`publish` on the running loop (no-op if no loop)."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.publish(event))
        except RuntimeError:
            logger.debug("Event bus publish skipped (no running loop): %s", event.kind.value)


# Process-wide default bus
bus = EventBus()


def get_event_bus() -> EventBus:
    return bus
