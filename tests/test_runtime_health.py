"""Deployment readiness is local, current and reflects Discord publication errors."""
import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bookclub.runtime_health import RuntimeHealth, bot_health, check_health, main


class RuntimeHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'runtime-health.json'
        self.now = 1000.0
        self.health = RuntimeHealth(self.path, deploy_sha='a' * 40, club_enabled=True,
                                    clock=lambda: self.now)

    def ready(self, guilds=(1,)):
        self.health.configure_club(guilds, 13)
        for guild_id in guilds:
            self.health.begin_reconciliation(guild_id)
            self.health.finish_reconciliation(guild_id)
        self.health.set_connected(True)
        return self.health.snapshot()

    def check(self, payload=None, **kwargs):
        return check_health(self.health.snapshot() if payload is None else payload,
                            now=self.now, expected_sha='a' * 40, **kwargs)

    def test_atomic_private_file_contains_operational_fields_only(self):
        self.ready()
        self.health.finish_reconciliation(1, ValueError('DISCORD_TOKEN=private and full essay text'))
        payload = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(payload['reconciliation']['guilds']['1']['error'], 'ValueError')
        self.assertEqual(set(payload), {'format', 'updated_at', 'started_at', 'pid', 'deploy_sha',
                                        'connected', 'club_enabled', 'schema_version', 'reconciliation'})
        self.assertNotIn('DISCORD_TOKEN', self.path.read_text())
        self.assertNotIn('essay text', self.path.read_text())
        self.assertGreater(payload['pid'], 0)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob('.runtime-health-*')), [])

    def test_replace_failure_preserves_complete_previous_file_and_cleans_temp(self):
        self.ready()
        previous = self.path.read_bytes()
        with patch('bookclub.runtime_health.os.replace', side_effect=OSError('sensitive value')), \
                self.assertLogs('bookclub.runtime_health', level='ERROR') as logs:
            self.health.connected = False
            self.assertFalse(self.health.write())
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob('.runtime-health-*')), [])
        self.assertNotIn('sensitive value', '\n'.join(logs.output))
        self.assertTrue(self.health.write())
        self.assertFalse(json.loads(self.path.read_text())['connected'])

    def test_readiness_requires_connection_release_freshness_and_process_generation(self):
        payload = self.ready()
        self.assertEqual(self.check(payload, require_club=True), [])
        self.assertIn('wrong-release', check_health(payload, expected_sha='b' * 40, now=self.now))
        self.assertIn('previous-process', self.check(payload, started_after=1001))
        self.assertEqual(self.check(payload, started_after=999), [])
        self.now += 91
        self.assertIn('stale-heartbeat', self.check(payload))
        self.health.set_connected(False)
        self.assertIn('discord-disconnected', self.check())

    def test_all_expected_guilds_need_a_successful_reconciliation(self):
        self.health.configure_club([1, 2], 13)
        self.health.set_connected(True)
        self.health.begin_reconciliation(1)
        self.health.finish_reconciliation(1)
        self.assertIn('reconciliation-pending', self.check())
        self.health.begin_reconciliation(2)
        self.health.finish_reconciliation(2)
        self.assertEqual(self.check(), [])
        self.health.finish_reconciliation(2, RuntimeError('private'))
        self.assertIn('reconciliation-failed', self.check())
        self.health.begin_reconciliation(2)
        self.assertIn('reconciliation-failed', self.check())
        self.health.finish_reconciliation(2)
        self.assertEqual(self.check(), [])

    def test_new_pass_does_not_hide_stalled_reconciliation_behind_fresh_heartbeat(self):
        self.ready()
        self.now += 301
        self.health.begin_reconciliation(1)
        self.health.write()
        self.assertIn('stale-reconciliation', self.check(max_club_age=300))
        self.health.finish_reconciliation(1)
        self.assertEqual(self.check(), [])

    def test_configuration_removes_disabled_guilds_and_does_not_reset_known_success(self):
        self.ready([1, 2])
        self.health.finish_reconciliation(2, RuntimeError())
        self.health.configure_club([1], 13)
        self.assertEqual(self.check(), [])
        self.assertEqual(set(self.health.snapshot()['reconciliation']['guilds']), {'1'})
        self.health.configure_club([1, 3], 13)
        self.assertIn('reconciliation-pending', self.check())

    def test_disabled_and_not_yet_configured_clubs_have_distinct_readiness(self):
        self.health.set_connected(True)
        self.assertIn('club-not-initialized', self.check())
        self.health.configure_club([], 13)
        self.assertEqual(self.check(require_club=True), [])
        disabled = RuntimeHealth(self.path, deploy_sha='a' * 40, clock=lambda: self.now)
        disabled.set_connected(True)
        self.assertEqual(check_health(disabled.snapshot(), now=self.now), [])
        self.assertIn('club-disabled', check_health(disabled.snapshot(), require_club=True, now=self.now))

    def test_invalid_or_future_timestamps_cannot_pass(self):
        payload = self.ready()
        for value in (None, True, '1000', float('nan'), float('inf'), -1):
            with self.subTest(value=value):
                self.assertEqual(self.check({**payload, 'updated_at': value}), ['invalid-timestamps'])
        self.assertIn('future-timestamp', self.check({**payload, 'updated_at': self.now + 60}))
        self.assertEqual(self.check([]), ['invalid-format'])

    def test_untrusted_error_text_is_not_persisted(self):
        self.ready()
        self.health.finish_reconciliation(1, 'private essay\nDISCORD_TOKEN=secret')
        self.assertEqual(self.health.guilds['1']['error'], 'ReconciliationError')

    def test_cli_reports_only_reason_codes_for_unreadable_or_unhealthy_file(self):
        self.path.write_text('{"secret":"private malformed data', encoding='utf-8')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--check', str(self.path)]), 1)
        self.assertEqual(output.getvalue(), 'unhealthy: heartbeat-unreadable\n')
        self.ready()
        with patch('bookclub.runtime_health.time.time', return_value=self.now), \
                redirect_stdout(output := io.StringIO()):
            self.assertEqual(main(['--check', str(self.path), '--require-club', '--expected-sha', 'a' * 40]), 0)
        self.assertEqual(output.getvalue(), 'healthy\n')

    def test_cli_checks_sha_from_environment_without_loading_dotenv(self):
        self.ready()
        with patch.dict(os.environ, RESHUFFLE_DEPLOY_SHA='b' * 40), \
                patch('bookclub.runtime_health.time.time', return_value=self.now), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--check', str(self.path)]), 1)

    def test_mock_bot_does_not_implicitly_create_health_tracker(self):
        self.assertIsNone(bot_health(Mock()))
        self.assertIs(bot_health(SimpleNamespace(runtime_health=self.health)), self.health)


class RuntimeHealthAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_does_not_hide_a_resumable_gateway_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            health = RuntimeHealth(Path(directory) / 'health.json')
            bot = SimpleNamespace(is_closed=Mock(side_effect=[False, True]), is_ready=lambda: True)
            with patch('bookclub.runtime_health.asyncio.sleep', new=AsyncMock()):
                await health._heartbeat(bot)
            self.assertFalse(health.snapshot()['connected'])

    async def test_heartbeat_marks_a_closed_bot_disconnected(self):
        with tempfile.TemporaryDirectory() as directory:
            health = RuntimeHealth(Path(directory) / 'health.json')
            health.set_connected(True)
            bot = SimpleNamespace(is_closed=lambda: True, is_ready=lambda: True)
            health.start(bot)
            await health._task
            self.assertFalse(health.snapshot()['connected'])

    async def test_worker_tracks_published_allowlisted_guilds_and_missing_guild(self):
        from bookclub.store import Store
        from bookclub.ui import Club
        try:
            from test_bookclub import CONFIG
        except ModuleNotFoundError:
            from tests.test_bookclub import CONFIG
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'bot.sqlite3')
            for guild_id in (1, 2, 3):
                store.configure(guild_id, CONFIG)
            store.set_published(1)
            store.set_published(2)
            health = RuntimeHealth(Path(directory) / 'health.json', club_enabled=True)
            health.set_connected(True)
            guild = SimpleNamespace(id=1)
            bot = SimpleNamespace(runtime_health=health, get_guild=lambda guild_id: guild if guild_id == 1 else None)
            cog = Club(bot, store, guild_ids={1, 3})
            cog.service.tick = AsyncMock(return_value=())
            await cog.worker.coro(cog)
            self.assertEqual(set(health.guilds), {'1'})
            self.assertEqual(check_health(health.snapshot(), require_club=True), [])
            cog.service.guild_ids = {1, 2, 3}
            await cog.worker.coro(cog)
            self.assertEqual(health.guilds['2']['error'], 'GuildUnavailable')
            self.assertIn('reconciliation-failed', check_health(health.snapshot()))
            cog.service.guild_ids = {1}
            cog.service.tick = AsyncMock(return_value=('cards.Forbidden',))
            await cog.worker.coro(cog)
            self.assertEqual(health.guilds['1']['error'], 'cards.Forbidden')
            cog.service.tick = AsyncMock(side_effect=ValueError('private message'))
            with self.assertLogs('bookclub.ui', level='ERROR'):
                await cog.worker.coro(cog)
            self.assertEqual(health.guilds['1']['error'], 'ValueError')
            cog.service.tick = AsyncMock(return_value=())
            await cog.worker.coro(cog)
            self.assertEqual(check_health(health.snapshot()), [])

    async def test_caught_card_and_essay_errors_make_tick_degraded(self):
        from bookclub.service import Service
        for failing in ('scan_essays', 'check_essays', 'refresh'):
            with self.subTest(failing=failing):
                store = Mock()
                store.settings.return_value = {'published': True}
                store.clock.return_value = 1000
                store.due_jobs.return_value = []
                service = Service(Mock(), store)
                for name in ('reconcile', 'live_participants', 'scan_essays', 'check_essays', 'refresh'):
                    setattr(service, name, AsyncMock(return_value=[]))
                service.book_updates.drain = AsyncMock()
                getattr(service, failing).side_effect = ValueError('private error')
                with self.assertLogs('bookclub.service', level='ERROR'):
                    errors = await service.tick(SimpleNamespace(id=1))
                self.assertEqual(errors, ('cards.ValueError' if failing == 'refresh' else 'essays.ValueError',))
                service.refresh.assert_awaited_once()

    async def test_disconnect_and_resume_hooks_write_state_without_network(self):
        try:
            from test_persistence import Shuffle
        except ModuleNotFoundError:
            from tests.test_persistence import Shuffle
        with tempfile.TemporaryDirectory() as directory:
            health = RuntimeHealth(Path(directory) / 'health.json')
            bot = SimpleNamespace(runtime_health=health)
            with patch.object(Shuffle, 'bot', bot):
                await Shuffle.on_resumed()
                self.assertTrue(health.snapshot()['connected'])
                await Shuffle.on_disconnect()
                self.assertFalse(health.snapshot()['connected'])


if __name__ == '__main__':
    unittest.main()
