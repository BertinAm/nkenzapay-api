"""Realtime fan-out.

Channels carries the events; this module is the single publish point so the
rest of the code never touches the channel layer directly. If the layer is
absent — a management command, a test — publishing is a no-op rather than an
error, because a transfer must never fail because a websocket was not there.
"""
from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def group_name(channel: str) -> str:
    """Channels group names allow a restricted character set."""
    return channel.replace(".", "_").replace("-", "_")


def publish(channel: str, event: str, payload: dict) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            group_name(channel),
            {"type": "broadcast", "event": event, "payload": payload},
        )
    except Exception as exc:  # noqa: BLE001 - delivery is best effort
        logger.warning("Could not publish %s on %s: %s", event, channel, exc)
