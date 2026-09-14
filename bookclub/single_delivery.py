"""Disable ambiguous retries for messages, forum creation and webhooks.

discord.py retries non-idempotent POSTs on 5xx and connection resets. A request
may already have created a message, so our durable publication recovery must
inspect Discord before deciding what happened. Only genuine 429 responses may
be retried here: Discord explicitly rejected those requests before delivery.

The serializer and rate-limit handling remain discord.py's. The HTTP session
seam is private, tested against discord.py 2.7.1; missing internals fail closed.
Copies share the original SDK rate-limit buckets and connection pool, while the
request guard belongs only to this call. Nothing globally patches the client.
"""
from __future__ import annotations

import asyncio
from copy import copy
import json

import aiohttp
from discord.http import HTTPClient

from .store import ClubError


class DeliveryUncertain(ClubError):
    """Discord may have accepted the request; reconcile instead of resending."""


_UNCERTAIN_ERRORS = (OSError, asyncio.TimeoutError, aiohttp.ClientError,
                     json.JSONDecodeError, UnicodeDecodeError)
_UNCERTAIN_TEXT = ('Discord не подтвердил отправку. Сообщение могло быть создано; '
                   'перед повтором нужно проверить сохранённую публикацию.')


class _GuardedRequest:
    def __init__(self, context):
        self.context = context

    async def __aenter__(self):
        try:
            response = await self.context.__aenter__()
        except _UNCERTAIN_ERRORS:
            raise DeliveryUncertain(_UNCERTAIN_TEXT) from None
        if response.status >= 500:
            # Enter failed from the caller's perspective, so we must release
            # this response ourselves. Do not read/log a potentially private
            # error body and do not let the SDK observe a retryable status.
            try:
                await self.context.__aexit__(None, None, None)
            except _UNCERTAIN_ERRORS:
                pass
            raise DeliveryUncertain(_UNCERTAIN_TEXT) from None
        return response

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            suppressed = await self.context.__aexit__(exc_type, exc, traceback)
        except _UNCERTAIN_ERRORS:
            raise DeliveryUncertain(_UNCERTAIN_TEXT) from None
        if isinstance(exc, _UNCERTAIN_ERRORS):
            # Covers a successful status followed by a lost/truncated body.
            # Raising a ClubError also bypasses the SDK's OSError retry loop.
            raise DeliveryUncertain(_UNCERTAIN_TEXT) from None
        return suppressed


class _SinglePostSession:
    def __init__(self, session):
        self.session = session

    def __getattr__(self, name):
        return getattr(self.session, name)

    def request(self, method, url, **kwargs):
        if method.upper() != 'POST':
            return self.session.request(method, url, **kwargs)
        # A 307/308 redirect would otherwise repeat the POST below the SDK's
        # retry loop. Discord's create endpoints do not require redirects.
        kwargs['allow_redirects'] = False
        try:
            context = self.session.request(method, url, **kwargs)
        except _UNCERTAIN_ERRORS:
            raise DeliveryUncertain(_UNCERTAIN_TEXT) from None
        return _GuardedRequest(context)


def _isolated_channel(channel):
    state = getattr(channel, '_state', None)
    http = getattr(state, 'http', None)
    session = getattr(http, '_HTTPClient__session', None)
    if not isinstance(http, HTTPClient) or not callable(getattr(session, 'request', None)):
        raise ClubError('Не удалось проверить безопасный транспорт Discord. Отправка не выполнена.')
    local_http = copy(http)
    local_http._HTTPClient__session = _SinglePostSession(session)
    local_state = copy(state)
    local_state.http = local_http
    local_channel = copy(channel)
    local_channel._state = local_state
    return local_channel, state


async def channel_send_once(channel, *args, **kwargs):
    """Send a bot message once, retaining nonce as additional protection."""
    local_channel, state = _isolated_channel(channel)
    message = await local_channel.send(*args, **kwargs)
    message._state = state
    message.channel = channel
    return message


async def create_thread_once(forum, **kwargs):
    """Create a forum thread without retrying an ambiguously accepted POST."""
    local_forum, state = _isolated_channel(forum)
    result = await local_forum.create_thread(**kwargs)
    # Returned resources should behave like ordinary SDK resources after this
    # isolated creation. Their future edits/reads use the original connection.
    result.thread._state = state
    result.message._state = state
    return result


async def webhook_send_once(webhook, *args, **kwargs):
    """Send a webhook message/thread without ambiguous automatic retries."""
    session = getattr(webhook, 'session', None)
    if not callable(getattr(session, 'request', None)):
        raise ClubError('Не удалось проверить безопасный транспорт Discord. Отправка не выполнена.')
    local_webhook = copy(webhook)
    local_webhook.session = _SinglePostSession(session)
    try:
        return await local_webhook.send(*args, **kwargs)
    finally:
        # WebhookMessage retains this copied webhook for later edits.
        local_webhook.session = session
