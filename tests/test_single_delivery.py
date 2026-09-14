"""Exercise real discord.py serializers/retry loops against a fake HTTP session."""
import asyncio
import io
import json
from types import SimpleNamespace
import unittest

import aiohttp
import discord
from discord.http import Route

from bookclub.single_delivery import DeliveryUncertain, channel_send_once, create_thread_once, webhook_send_once
from bookclub.store import ClubError


def message_data():
    return {'id': '101', 'channel_id': '101', 'guild_id': '1', 'type': 0,
            'content': 'Essay without visible codes', 'attachments': [], 'embeds': [],
            'author': {'id': '99', 'username': 'Author', 'discriminator': '0', 'avatar': None}}


def thread_data():
    return {'id': '101', 'parent_id': '13', 'owner_id': '99', 'name': 'Book', 'type': 11,
            'message_count': 1, 'member_count': 1,
            'thread_metadata': {'archived': False, 'auto_archive_duration': 1440,
                                'archive_timestamp': '2026-01-01T00:00:00+00:00'},
            'message': message_data()}


class Response:
    def __init__(self, status, body, *, headers=None, read_error=None):
        self.status, self.body, self.read_error = status, body, read_error
        self.headers = {'content-type': 'application/json', **(headers or {})}
        self.reason = 'Fake response'
        self.read_count = 0

    async def text(self, **kwargs):
        self.read_count += 1
        if self.read_error:
            raise self.read_error
        return json.dumps(self.body)


class RequestContext:
    def __init__(self, result, session):
        self.result, self.session = result, session

    async def __aenter__(self):
        if self.session.gate is not None:
            self.session.entered.set()
            await self.session.gate.wait()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def __aexit__(self, exc_type, exc, traceback):
        self.session.released += 1
        return False


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.requests, self.released, self.accepted = [], 0, 0
        self.gate, self.entered, self.closed = None, asyncio.Event(), False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.results:
            raise AssertionError('Unexpected retry of an ambiguously accepted POST')
        result = self.results.pop(0)
        if isinstance(result, BaseException) or result.status < 400 or result.status >= 500:
            self.accepted += 1
        return RequestContext(result, self)

    async def close(self):
        self.closed = True


class SingleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = discord.Client(intents=discord.Intents.none())
        await self.client._async_setup_hook()
        self.state = self.client._connection
        self.client.http._global_over = asyncio.Event()
        self.client.http._global_over.set()
        self.guild = discord.Guild(state=self.state, data={'id': '1', 'name': 'Guild', 'owner_id': '99'})
        self.state._guilds[1] = self.guild
        self.forum = discord.ForumChannel(state=self.state, guild=self.guild,
                                         data={'id': '13', 'name': 'Books', 'type': 15, 'position': 0})
        self.guild._channels[13] = self.forum
        self.channel = discord.TextChannel(state=self.state, guild=self.guild,
                                           data={'id': '11', 'name': 'News', 'type': 0, 'position': 0})
        self.guild._channels[11] = self.channel

    async def asyncTearDown(self):
        await self.client.close()

    def transport(self, results):
        session = Session(results)
        self.client.http._HTTPClient__session = session
        webhook = discord.Webhook({'id': '23', 'type': 1, 'token': 'private-test-token',
                                   'guild_id': '1', 'channel_id': '13'}, session, state=self.state)
        return session, webhook

    async def test_forum_accepted_500_does_not_retry_or_read_error_body(self):
        response = Response(500, {'private': 'must never be logged'})
        session, _ = self.transport([response])
        with self.assertRaises(DeliveryUncertain) as raised:
            await create_thread_once(self.forum, name='Book', content='private essay')
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(session.accepted, 1)
        self.assertEqual(session.released, 1)
        self.assertEqual(response.read_count, 0)
        self.assertNotIn('private', str(raised.exception))
        self.assertIs(self.forum._state, self.state)
        self.assertIs(self.client.http._HTTPClient__session, session)

    async def test_webhook_accepted_5xx_never_retries(self):
        for status in (500, 502, 503, 504, 524):
            with self.subTest(status=status):
                session, webhook = self.transport([Response(status, {'private': 'essay'})])
                with self.assertRaises(DeliveryUncertain) as raised:
                    await webhook_send_once(webhook, 'private essay', wait=True)
                self.assertEqual(len(session.requests), 1)
                self.assertEqual(session.accepted, 1)
                self.assertIs(webhook.session, session)
                self.assertNotIn('private-test-token', str(raised.exception))
                self.assertNotIn('private essay', str(raised.exception))

    async def test_connection_reset_after_acceptance_is_not_retried_by_sdk(self):
        for target in ('forum', 'webhook', 'channel'):
            with self.subTest(target=target):
                session, webhook = self.transport([ConnectionResetError(10054, 'private URL/token/body')])
                with self.assertRaises(DeliveryUncertain) as raised:
                    if target == 'forum':
                        await create_thread_once(self.forum, name='Book', content='essay')
                    elif target == 'channel':
                        await channel_send_once(self.channel, 'essay', nonce='123')
                    else:
                        await webhook_send_once(webhook, 'essay', wait=True)
                self.assertEqual(len(session.requests), 1)
                self.assertEqual(session.accepted, 1)
                self.assertNotIn('private URL', str(raised.exception))

    async def test_success_status_with_lost_or_truncated_response_body_is_uncertain_once(self):
        for target in ('forum', 'webhook', 'channel'):
            for error in (ConnectionResetError(54, 'private response'), asyncio.TimeoutError(),
                          aiohttp.ClientPayloadError('private body'),
                          json.JSONDecodeError('private JSON', 'private JSON', 0)):
                with self.subTest(target=target, error=type(error).__name__):
                    session, webhook = self.transport([Response(200, {}, read_error=error)])
                    with self.assertRaises(DeliveryUncertain):
                        if target == 'forum':
                            await create_thread_once(self.forum, name='Book', content='essay')
                        elif target == 'channel':
                            await channel_send_once(self.channel, 'essay', nonce='123')
                        else:
                            await webhook_send_once(webhook, 'essay', wait=True)
                    self.assertEqual(len(session.requests), 1)
                    self.assertEqual(session.released, 1)

    async def test_bot_accepted_500_stops_before_later_rate_limit_can_expire_nonce(self):
        session, _ = self.transport([
            Response(500, {'private': 'response after message acceptance'}),
            Response(429, {'retry_after': 3600}, headers={'Via': '1.1 discord'}),
            Response(200, message_data()),
        ])
        with self.assertRaises(DeliveryUncertain):
            await channel_send_once(self.channel, 'essay', nonce='123')
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(session.accepted, 1)
        self.assertEqual(len(session.results), 2)
        self.assertIs(self.channel._state, self.state)
        self.assertIs(self.client.http._HTTPClient__session, session)

    async def test_bot_success_keeps_nonce_buttons_mentions_and_original_channel_state(self):
        thread = discord.Thread(guild=self.guild, state=self.state, data=thread_data())
        for channel in (self.channel, thread):
            with self.subTest(channel=type(channel).__name__):
                data = {**message_data(), 'channel_id': str(channel.id)}
                session, _ = self.transport([Response(200, data)])
                view = discord.ui.View(timeout=None)
                view.add_item(discord.ui.Button(label='Open', custom_id='catalog-control'))
                result = await channel_send_once(channel, 'essay', nonce='987654321', view=view,
                                                 allowed_mentions=discord.AllowedMentions.none())
                self.assertIs(result._state, self.state)
                self.assertIs(result.channel, channel)
                self.assertIs(channel._state, self.state)
                self.assertIs(self.client.http._HTTPClient__session, session)
                payload = json.loads(session.requests[0][2]['data'])
                self.assertEqual(payload['content'], 'essay')
                self.assertEqual(payload['nonce'], '987654321')
                self.assertTrue(payload['enforce_nonce'])
                self.assertEqual(payload['components'][0]['components'][0]['custom_id'], 'catalog-control')
                self.assertEqual(payload['allowed_mentions']['parse'], [])
                self.assertFalse(session.requests[0][2]['allow_redirects'])
                self.assertIn(view, self.state.persistent_views)

    async def test_forum_success_preserves_serializer_tags_view_mentions_and_state(self):
        session, _ = self.transport([Response(201, thread_data())])
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label='Open', custom_id='book-control'))
        result = await create_thread_once(self.forum, name='Book', content='essay', view=view,
                                          applied_tags=[discord.Object(17)], allowed_mentions=discord.AllowedMentions.none())
        self.assertEqual(result.thread.id, 101)
        self.assertEqual(result.message.id, 101)
        self.assertIs(result.thread._state, self.state)
        self.assertIs(result.message._state, self.state)
        self.assertIs(self.client.http._HTTPClient__session, session)
        payload = json.loads(session.requests[0][2]['data'])
        self.assertEqual(payload['applied_tags'], ['17'])
        self.assertEqual(payload['message']['content'], 'essay')
        self.assertEqual(payload['message']['components'][0]['components'][0]['custom_id'], 'book-control')
        self.assertEqual(payload['message']['allowed_mentions']['parse'], [])
        self.assertFalse(session.requests[0][2]['allow_redirects'])
        self.assertIn(view, self.state.persistent_views)

    async def test_webhook_success_preserves_identity_tags_thread_and_message_state(self):
        session, webhook = self.transport([Response(200, message_data())])
        result = await webhook_send_once(webhook, 'essay', username='Author', avatar_url='https://example.org/avatar',
                                         thread_name='Book', applied_tags=[discord.Object(17)], wait=True,
                                         allowed_mentions=discord.AllowedMentions.none())
        self.assertEqual(result.id, 101)
        self.assertIs(result._state._webhook.session, session)
        self.assertIs(webhook.session, session)
        payload = json.loads(session.requests[0][2]['data'])
        self.assertEqual(payload['username'], 'Author')
        self.assertEqual(payload['avatar_url'], 'https://example.org/avatar')
        self.assertEqual(payload['thread_name'], 'Book')
        self.assertEqual(payload['applied_tags'], [17])
        self.assertEqual(payload['allowed_mentions']['parse'], [])
        self.assertEqual(session.requests[0][2]['params']['wait'], 1)

    async def test_files_use_sdk_multipart_and_survive_genuine_429_retry(self):
        for target in ('forum', 'webhook', 'channel'):
            with self.subTest(target=target):
                data = thread_data() if target == 'forum' else message_data()
                session, webhook = self.transport([
                    Response(429, {'retry_after': 0}, headers={'Via': '1.1 discord'}), Response(200, data)])
                file = discord.File(io.BytesIO(b'private attachment'), filename='essay.txt')
                if target == 'forum':
                    await create_thread_once(self.forum, name='Book', content='essay', file=file)
                elif target == 'channel':
                    await channel_send_once(self.channel, 'essay', file=file, nonce='123')
                else:
                    await webhook_send_once(webhook, 'essay', file=file, wait=True)
                self.assertEqual(len(session.requests), 2)
                self.assertEqual(session.accepted, 1)
                for request in session.requests:
                    form = request[2]['data']
                    self.assertIsInstance(form, aiohttp.FormData)
                    names = [field[0]['name'] for field in form._fields]
                    self.assertEqual(names, ['payload_json', 'files[0]'])

    async def test_untrusted_429_is_not_retried(self):
        for target in ('forum', 'webhook', 'channel'):
            session, webhook = self.transport([Response(429, 'CDN error')])
            with self.assertRaises(discord.HTTPException):
                if target == 'forum':
                    await create_thread_once(self.forum, name='Book', content='essay')
                elif target == 'channel':
                    await channel_send_once(self.channel, 'essay', nonce='123')
                else:
                    await webhook_send_once(webhook, 'essay', wait=True)
            self.assertEqual(len(session.requests), 1)
            self.assertEqual(session.accepted, 0)

    async def test_missing_private_session_fails_before_request(self):
        with self.assertRaisesRegex(ClubError, 'Отправка не выполнена'):
            await create_thread_once(SimpleNamespace(_state=SimpleNamespace(http=None)), name='Book', content='essay')
        with self.assertRaisesRegex(ClubError, 'Отправка не выполнена'):
            await webhook_send_once(SimpleNamespace(session=None), 'essay')
        with self.assertRaisesRegex(ClubError, 'Отправка не выполнена'):
            await channel_send_once(SimpleNamespace(_state=SimpleNamespace(http=None)), 'essay')

    async def test_concurrent_normal_request_keeps_original_client_session(self):
        session, _ = self.transport([Response(200, thread_data()), Response(200, {'ok': True})])
        session.gate = asyncio.Event()
        creation = asyncio.create_task(create_thread_once(self.forum, name='Book', content='essay'))
        await asyncio.wait_for(session.entered.wait(), 0.5)
        self.assertIs(self.client.http._HTTPClient__session, session)
        self.assertIs(self.forum._state, self.state)
        ordinary = asyncio.create_task(self.client.http.request(Route('GET', '/gateway')))
        session.gate.set()
        result, response = await asyncio.gather(creation, ordinary)
        self.assertEqual(result.thread.id, 101)
        self.assertEqual(response, {'ok': True})
        self.assertEqual([request[0] for request in session.requests], ['POST', 'GET'])


if __name__ == '__main__':
    unittest.main()
