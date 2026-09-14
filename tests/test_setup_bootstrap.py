"""Local registration and bootstrap coverage without a Discord connection."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

from bookclub.store import ClubError, Store
from bookclub.ui import Club
from test_bookclub import CONFIG


class SetupBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'voice_activity.sqlite3'
        self.store = Store(self.path)

    async def test_empty_database_can_boot_without_json(self):
        from test_persistence import Shuffle
        async with commands.Bot(command_prefix='!', intents=discord.Intents.default()) as bot:
            with patch.object(Shuffle, 'bot', bot), patch.object(Shuffle, 'VOICE_STATS_DB_FILE', str(self.path)), \
                    patch.dict(os.environ, BOOKCLUB_ENABLED='true', BOOKCLUB_CONFIG_FILE=''), \
                    patch('bookclub.config.load_config') as load_config:
                await Shuffle.setup_hook()
                cog = bot.get_cog('Club')
                self.assertIsNotNone(cog)
                self.assertIsNone(cog.service.guild_ids)
                self.assertEqual(cog.store.rows('SELECT * FROM bc_settings'), [])
                self.assertIsNotNone(bot.tree.get_command('club').get_command('setup'))
                load_config.assert_not_called()
                await bot.remove_cog('Club')

    async def test_saved_configuration_survives_boot_without_json(self):
        from test_persistence import Shuffle
        self.store.configure(1, CONFIG)
        self.store.set_published(1)
        before = self.store.settings(1)
        async with commands.Bot(command_prefix='!', intents=discord.Intents.default()) as bot:
            with patch.object(Shuffle, 'bot', bot), patch.object(Shuffle, 'VOICE_STATS_DB_FILE', str(self.path)), \
                    patch.dict(os.environ, BOOKCLUB_ENABLED='true', BOOKCLUB_CONFIG_FILE=''):
                await Shuffle.setup_hook()
                cog = bot.get_cog('Club')
                self.assertIsNone(cog.service.guild_ids)
                self.assertEqual(cog.store.settings(1), before)
                await bot.remove_cog('Club')

    async def test_json_keeps_explicit_allowlist_and_uses_import_snapshot(self):
        from test_persistence import Shuffle
        self.store.configure(2, CONFIG)
        config_file = Path(self.tmp.name) / 'bookclub.json'
        config_file.write_text(json.dumps({'guilds': {'1': CONFIG}}), encoding='utf-8')
        async with commands.Bot(command_prefix='!', intents=discord.Intents.default()) as bot:
            with patch.object(Shuffle, 'bot', bot), patch.object(Shuffle, 'VOICE_STATS_DB_FILE', str(self.path)), \
                    patch.dict(os.environ, BOOKCLUB_ENABLED='true', BOOKCLUB_CONFIG_FILE=str(config_file)):
                await Shuffle.setup_hook()
                cog = bot.get_cog('Club')
                self.assertEqual(cog.service.guild_ids, {1})
                self.assertEqual(cog.store.settings(1)['books'], CONFIG['books'])
                self.assertEqual(cog.store.settings(2)['books'], CONFIG['books'])
                cog.store.configure(1, {**cog.store.settings(1), 'books': 333})
                await bot.remove_cog('Club')
                await Shuffle.setup_hook()
                self.assertEqual(bot.get_cog('Club').store.settings(1)['books'], 333)
                self.assertEqual(bot.get_cog('Club').service.guild_ids, {1})
                await bot.remove_cog('Club')

    async def test_empty_json_allowlist_does_not_reenable_saved_guilds(self):
        from test_persistence import Shuffle
        self.store.configure(1, CONFIG)
        config_file = Path(self.tmp.name) / 'bookclub.json'
        config_file.write_text('{"guilds": {}}', encoding='utf-8')
        async with commands.Bot(command_prefix='!', intents=discord.Intents.default()) as bot:
            with patch.object(Shuffle, 'bot', bot), patch.object(Shuffle, 'VOICE_STATS_DB_FILE', str(self.path)), \
                    patch.dict(os.environ, BOOKCLUB_ENABLED='true', BOOKCLUB_CONFIG_FILE=str(config_file)):
                await Shuffle.setup_hook()
                self.assertEqual(bot.get_cog('Club').service.guild_ids, set())
                self.assertEqual(bot.get_cog('Club').store.settings(1)['books'], CONFIG['books'])
                await bot.remove_cog('Club')

    def context(self, cog, command=None, interaction=True):
        return SimpleNamespace(command=command or cog.setup, guild=SimpleNamespace(id=1),
                               author=SimpleNamespace(id=99), interaction=object() if interaction else None,
                               defer=AsyncMock())

    async def test_setup_before_invoke_uses_bootstrap_guard_without_settings(self):
        cog = Club(Mock(), self.store)
        cog.service.setup_actor = AsyncMock()
        cog.service.actor = AsyncMock(side_effect=ClubError('not configured'))
        ctx = self.context(cog)
        await cog.cog_before_invoke(ctx)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        cog.service.setup_actor.assert_awaited_once_with(ctx.guild, ctx.author.id)
        cog.service.actor.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT * FROM bc_settings'), [])

    async def test_setup_guard_also_recognizes_qualified_command_name(self):
        cog = Club(Mock(), self.store)
        cog.service.setup_actor = AsyncMock()
        cog.service.actor = AsyncMock()
        ctx = self.context(cog, command=SimpleNamespace(qualified_name='club setup'), interaction=False)
        await cog.cog_before_invoke(ctx)
        cog.service.setup_actor.assert_awaited_once_with(ctx.guild, 99)
        cog.service.actor.assert_not_awaited()
        ctx.defer.assert_not_awaited()

    async def test_existing_commands_keep_normal_guard(self):
        cog = Club(Mock(), self.store)
        cog.service.setup_actor = AsyncMock()
        cog.service.actor = AsyncMock(side_effect=ClubError('not configured'))
        ctx = self.context(cog, command=cog.books)
        with self.assertRaisesRegex(ClubError, 'not configured'):
            await cog.cog_before_invoke(ctx)
        cog.service.actor.assert_awaited_once_with(ctx.guild, 99, require_access=True)
        cog.service.setup_actor.assert_not_awaited()

    async def test_setup_callback_forwards_flags_and_displays_report(self):
        cog = Club(Mock(), self.store)
        cog.service.setup_server = AsyncMock(return_value=['Каналы проверены.', 'Настройка сохранена.'])
        cog.say = AsyncMock()
        ctx = self.context(cog)
        await cog.setup.callback(cog, ctx, check_only=True, retry_missing=True, repair_permissions=True)
        cog.service.setup_server.assert_awaited_once_with(ctx.guild, 99, check_only=True, retry_missing=True, category=None, repair_permissions=True)
        cog.say.assert_awaited_once_with(ctx, 'Каналы проверены.\nНастройка сохранена.')

    async def test_setup_default_flags_do_not_retry_unknown_creations(self):
        cog = Club(Mock(), self.store)
        cog.service.setup_server = AsyncMock(return_value=['Готово.'])
        cog.say = AsyncMock()
        ctx = self.context(cog)
        await cog.setup.callback(cog, ctx)
        cog.service.setup_server.assert_awaited_once_with(ctx.guild, 99, check_only=False, retry_missing=False, category=None, repair_permissions=False)

    async def test_setup_registration_fits_discord_limit_and_preserves_autocomplete(self):
        async with commands.Bot(command_prefix='!', intents=discord.Intents.default()) as bot:
            cog = Club(bot, self.store)
            await bot.add_cog(cog)
            payload = bot.tree.get_command('club').to_dict(bot.tree)
            self.assertLessEqual(len(payload['options']), 25)
            setup = next(option for option in payload['options'] if option['name'] == 'setup')
            self.assertEqual({option['name'] for option in setup['options']}, {'check_only', 'retry_missing', 'category', 'repair_permissions'})
            for parameter in setup['options']:
                expected = discord.AppCommandOptionType.channel if parameter['name'] == 'category' else discord.AppCommandOptionType.boolean
                self.assertEqual(parameter['type'], expected.value)
                if parameter['name'] == 'category':
                    self.assertEqual(parameter['channel_types'], [discord.ChannelType.category.value])
                self.assertFalse(parameter.get('required', False))
            for option in payload['options']:
                for parameter in option.get('options', []):
                    if parameter['name'] in ('book', 'meeting', 'event'):
                        self.assertTrue(parameter['autocomplete'])
            await bot.remove_cog('Club')


if __name__ == '__main__':
    unittest.main()
