"""Server-side pub/sub hub for real-time event distribution.

Central registry for active client subscribers (SSE) and dashboard connections
(WebSocket). Components publish events; transports subscribe.

Multiple subscribers per (user, channel) are supported — the previous
single-subscriber-per-key model silently evicted the first subscriber when a
second daemon connected (e.g. Alex's laptop + work Mac running the same
user). Now each subscribe() appends a new entry and returns a queue identity
the caller passes back to unsubscribe(); publish/broadcast fan out to all
matching subscribers.

Usage:
    from app.stream_manager import stream_manager

    # Publishing (from scheduler, backlog sync, etc.)
    await stream_manager.publish(event, target_user="alex")
    await stream_manager.broadcast(event)

    # Subscribing (from SSE handler or WebSocket)
    queue = await stream_manager.subscribe(user="alex", channel="sse")
    ...
    await stream_manager.unsubscribe(user="alex", channel="sse", queue=queue)
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Subscriber:
    """A connected subscriber (SSE client or WebSocket dashboard)."""

    user: str
    channel: str  # "sse" or "websocket"
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=100))


class StreamManager:
    """Thread-safe pub/sub hub for server-push events."""

    def __init__(self):
        # Each (user, channel) key maps to a list of Subscribers. Two daemons
        # subscribing as the same user both get their own queue; publish()
        # fans out to all of them.
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._lock = asyncio.Lock()
        # Captured in main.py lifespan so sync code (tool handlers running in
        # asyncio.to_thread) can dispatch coroutines back onto the loop via
        # asyncio.run_coroutine_threadsafe.
        self.loop: asyncio.AbstractEventLoop | None = None

    def _key(self, user: str, channel: str) -> str:
        return f"{user}:{channel}"

    async def subscribe(self, user: str, channel: str) -> asyncio.Queue:
        """Register a subscriber and return its event queue.

        Multiple subscribers with the same (user, channel) coexist — each
        gets its own queue. Pass the returned queue back to unsubscribe()
        so the right one is removed when the connection closes.
        """
        key = self._key(user, channel)
        async with self._lock:
            sub = Subscriber(user=user, channel=channel)
            self._subscribers.setdefault(key, []).append(sub)
            count = len(self._subscribers[key])
            logger.info(
                "Subscriber connected: %s (%d total for this key)", key, count
            )
            return sub.queue

    async def unsubscribe(
        self,
        user: str,
        channel: str,
        queue: asyncio.Queue,
    ) -> None:
        """Remove a subscriber.

        `queue` identifies which subscriber to remove (multiple may exist
        for the same key) — always the queue returned by subscribe().
        """
        key = self._key(user, channel)
        async with self._lock:
            subs = self._subscribers.get(key, [])
            before = len(subs)
            subs[:] = [s for s in subs if s.queue is not queue]
            removed = before - len(subs)
            if not subs:
                self._subscribers.pop(key, None)
            if removed:
                logger.info(
                    "Subscriber disconnected: %s (%d remain)", key, len(subs)
                )

    async def publish(self, event: dict, target_user: str | None = None) -> int:
        """Publish an event to a specific user's subscribers (all of them).

        Fans out to every subscriber matching `target_user` across every
        channel for that user. Returns the number of subscribers that
        received the event.
        """
        count = 0
        async with self._lock:
            for subs in self._subscribers.values():
                for sub in subs:
                    if target_user and sub.user != target_user:
                        continue
                    try:
                        sub.queue.put_nowait(event)
                        count += 1
                    except asyncio.QueueFull:
                        logger.warning(
                            "Queue full for %s:%s, dropping event",
                            sub.user,
                            sub.channel,
                        )
        return count

    async def broadcast(self, event: dict) -> int:
        """Publish an event to ALL subscribers."""
        return await self.publish(event, target_user=None)

    @property
    def subscriber_count(self) -> int:
        return sum(len(subs) for subs in self._subscribers.values())

    def connected_users(self) -> list[str]:
        """Return list of users with at least one active client (SSE) stream."""
        users: set[str] = set()
        for subs in self._subscribers.values():
            for sub in subs:
                if sub.channel == "sse":
                    users.add(sub.user)
        return sorted(users)

    @staticmethod
    def new_message_id() -> str:
        """Generate a unique message ID for server-push messages."""
        return str(uuid.uuid4())[:8]


# Singleton instance — import this
stream_manager = StreamManager()
