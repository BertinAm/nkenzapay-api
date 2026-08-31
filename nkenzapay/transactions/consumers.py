"""Websocket consumers.

Every subscription is authorised on the server. A customer may join their own
transfers and their own user channel and nothing else; the desk queue is open
only to accounts with an AdminUser row.
"""
from __future__ import annotations

import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .realtime import group_name


class ChannelConsumer(AsyncWebsocketConsumer):
    """One socket, one channel, decided by the URL and checked against the user."""

    async def connect(self):
        self.user = self.scope.get("user")
        self.channel_key = await self.resolve_channel()
        if self.channel_key is None:
            await self.close(code=4403)
            return
        self.group = group_name(self.channel_key)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if getattr(self, "group", None):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        """The only thing a client may push is a typing ping. Messages go
        through the REST endpoint so they are validated and persisted once."""
        if not text_data:
            return
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return
        if data.get("type") == "typing" and self.channel_key.startswith("transaction."):
            await self.channel_layer.group_send(
                self.group,
                {
                    "type": "broadcast",
                    "event": "typing",
                    "payload": {"from_desk": bool(getattr(self.user, "is_desk", False))},
                },
            )

    async def broadcast(self, message):
        await self.send(text_data=json.dumps({
            "event": message["event"],
            "payload": message["payload"],
        }))

    async def resolve_channel(self):
        user = self.user
        if user is None or not user.is_authenticated:
            return None

        kind = self.scope["url_route"]["kwargs"].get("kind")
        key = self.scope["url_route"]["kwargs"].get("key", "")

        if kind == "transaction":
            allowed = await self.may_read_transaction(user, key)
            return f"transaction.{key}" if allowed else None
        if kind == "user":
            return f"user.{user.id}"
        if kind == "admin":
            return "admin.queue" if await self.is_desk(user) else None
        if kind == "rates":
            return "rates"
        return None

    @database_sync_to_async
    def may_read_transaction(self, user, reference):
        from nkenzapay.accounts.models import AdminUser
        from .models import Transaction

        if AdminUser.objects.filter(user=user).exists():
            return Transaction.objects.filter(reference=reference).exists()
        return Transaction.objects.filter(reference=reference, user=user).exists()

    @database_sync_to_async
    def is_desk(self, user):
        from nkenzapay.accounts.models import AdminUser

        return AdminUser.objects.filter(user=user).exists()
