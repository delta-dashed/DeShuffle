"""Import commands keep account codes and archived text in private interactions."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext import commands

from bookclub.store import ClubError, Store
from bookclub.ui import Club
from test_bookclub import CONFIG


class ImportUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'club.db')
        self.store.configure(1, CONFIG)
        self.cog = Club(Mock(), self.store)
        self.cog.importer = SimpleNamespace(guard=AsyncMock(), login=AsyncMock(return_value={
            'verification_uri': 'https://auth.openai.com/codex/device', 'user_code': 'ABCD-EFGH'}),
            scan=AsyncMock(return_value={'id': 'run'}), apply=AsyncMock(return_value=['Готово']),
            status=AsyncMock(return_value=['Нет расхода']), review=AsyncMock(return_value={'id': 'run'}))
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id = 41
        self.ctx = SimpleNamespace(guild=SimpleNamespace(id=1), author=SimpleNamespace(id=99), channel=self.channel,
            interaction=SimpleNamespace(id=777, followup=SimpleNamespace(send=AsyncMock())))

    async def test_device_code_is_only_sent_as_ephemeral_reply(self):
        await self.cog.import_login.callback(self.cog, self.ctx)
        reply = self.ctx.interaction.followup.send.await_args
        self.assertTrue(reply.kwargs['ephemeral'])
        self.assertIn('ABCD-EFGH', reply.args[0])
        self.assertEqual(reply.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_prefix_command_never_starts_login_or_exposes_code(self):
        self.ctx.interaction = None
        with self.assertRaises(ClubError):
            await self.cog.import_login.callback(self.cog, self.ctx)
        self.cog.importer.login.assert_not_awaited()

    async def test_rejected_guard_prevents_login(self):
        self.cog.importer.guard.side_effect = ClubError('Отключено')
        with self.assertRaises(ClubError):
            await self.cog.import_login.callback(self.cog, self.ctx)
        self.cog.importer.login.assert_not_awaited()
        self.ctx.interaction.followup.send.assert_not_awaited()

    async def test_scan_defaults_to_current_channel_and_uses_interaction_identity(self):
        self.cog.show_import = AsyncMock()
        await self.cog.import_scan.callback(self.cog, self.ctx)
        self.cog.importer.scan.assert_awaited_once_with(self.ctx.guild, 99, 41, '777',
                                                       before_id=None, thread_id=None, after_id=None)
        self.cog.show_import.assert_awaited_once()

    async def test_scan_inside_thread_targets_that_thread(self):
        thread = Mock(spec=discord.Thread)
        thread.id, thread.parent = 51, self.channel
        self.ctx.channel = thread
        self.cog.show_import = AsyncMock()
        await self.cog.import_scan.callback(self.cog, self.ctx, after='52')
        self.cog.importer.scan.assert_awaited_once_with(self.ctx.guild, 99, 41, '777',
                                                       before_id=None, thread_id=51, after_id=52)

    async def test_invalid_cursor_does_not_start_analysis(self):
        with self.assertRaises(ClubError):
            await self.cog.import_scan.callback(self.cog, self.ctx, before='-1')
        self.cog.importer.scan.assert_not_awaited()

    async def test_apply_default_requires_confirmation_in_service(self):
        await self.cog.import_apply.callback(self.cog, self.ctx, 'run')
        self.cog.importer.apply.assert_awaited_once_with(self.ctx.guild, 99, 'run', confirm=False)

    async def test_nested_import_commands_fit_discord_limit(self):
        async with commands.Bot(command_prefix='!', intents=discord.Intents.default()) as bot:
            cog = Club(bot, self.store)
            await bot.add_cog(cog)
            try:
                payload = bot.tree.get_command('club').to_dict(bot.tree)
                self.assertLessEqual(len(payload['options']), 25)
                group = next(option for option in payload['options'] if option['name'] == 'import')
                self.assertEqual(group['type'], discord.AppCommandOptionType.subcommand_group.value)
                self.assertTrue({'login', 'status', 'scan', 'review', 'apply', 'preview', 'restore'} <=
                                {c['name'] for c in group['options']})
            finally:
                await bot.remove_cog('Club')


if __name__ == '__main__':
    unittest.main()
