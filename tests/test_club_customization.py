"""Manual Discord customization survives reconciliation and process restarts."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from bookclub.catalog_ui import CatalogView
from bookclub.provision import DESCRIPTIONS, OLD_DESCRIPTIONS, Provisioner
from bookclub.render import news_content
from bookclub.service import Service
from bookclub.store import Store
from bookclub.ui import Club
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness
from test_setup_discord import SetupHarness


class ClubCustomizationTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service
        self.store.set_published(1)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def restart(self):
        self.store = Store(self.path, clock=lambda: self.now)
        with self.store.tx() as db:
            db.execute('UPDATE bc_publications SET content_hash=NULL')
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service

    async def test_manual_book_and_catalog_titles_survive_restart_and_book_edit(self):
        await self.service.refresh(self.h.guild)
        book_pub = self.store.publication(f'book:{self.book["id"]}')
        catalog_pub = self.store.publication('catalog:1')
        book_thread = self.h.channels[book_pub['channel_id']]
        catalog_thread = self.h.channels[catalog_pub['channel_id']]
        book_thread.name = 'Наше необычное название обсуждения'
        catalog_thread.name = 'Полка клуба'
        book_thread.edit.reset_mock()
        catalog_thread.edit.reset_mock()
        count = self.h.channels[13].create_thread.await_count
        self.restart()
        self.store.update_book(1, self.book['id'], title='Уточнённое название книги', author='Уточнённый автор')
        await self.service.refresh(self.h.guild)
        self.assertEqual(book_thread.name, 'Наше необычное название обсуждения')
        self.assertEqual(catalog_thread.name, 'Полка клуба')
        self.assertEqual(self.store.publication(f'book:{self.book["id"]}')['channel_id'], book_thread.id)
        self.assertEqual(self.store.publication('catalog:1')['channel_id'], catalog_thread.id)
        self.assertEqual(self.h.channels[13].create_thread.await_count, count)
        self.assertIn('Уточнённое название книги', book_thread.messages[book_pub['message_id']].content)
        self.assertFalse(any('name' in call.kwargs for call in book_thread.edit.await_args_list))
        self.assertFalse(any('name' in call.kwargs for call in catalog_thread.edit.await_args_list))

    async def test_default_book_topic_follows_book_title_after_restart(self):
        await self.service.refresh(self.h.guild)
        pub = self.store.publication(f'book:{self.book["id"]}')
        self.assertEqual(pub['managed_name'], 'Книга · Автор')
        thread = self.h.channels[pub['channel_id']]
        self.restart()
        self.store.update_book(1, self.book['id'], title='Новое название')
        await self.service.refresh(self.h.guild)
        self.assertEqual(thread.name, 'Новое название · Автор')
        self.assertEqual(self.store.publication(f'book:{self.book["id"]}')['managed_name'], thread.name)

    async def test_migrated_publication_without_name_metadata_preserves_existing_custom_title(self):
        await self.service.refresh(self.h.guild)
        pub = self.store.publication(f'book:{self.book["id"]}')
        thread = self.h.channels[pub['channel_id']]
        thread.name = 'Название до обновления бота'
        with self.store.tx() as db:
            db.execute('UPDATE bc_publications SET managed_name=NULL,content_hash=NULL WHERE key=?', (pub['key'],))
        await self.service.refresh(self.h.guild)
        self.assertEqual(thread.name, 'Название до обновления бота')

    async def test_news_is_static_when_book_state_deadline_and_meeting_date_change(self):
        settings = self.store.settings(1)
        before = news_content(self.store, 1, settings)
        await self.service.refresh(self.h.guild)
        publication = self.store.publication('news:1')
        news_card = self.h.channels[11].messages[publication['message_id']]
        manual = self.h.message(self.h.channels[11], 77777, 'Встречу перенесли на воскресенье, начало в 16:00.')
        manual.author = self.h.members[99]
        self.h.channels[11].send.reset_mock()
        news_card.edit.reset_mock()
        self.store.update_book(1, self.book['id'], title='Новый заголовок', status='reading', deadline=self.now + 90000)
        self.sync(start=self.now + 500000, end=self.now + 505400)
        await self.service.refresh(self.h.guild)
        self.assertEqual(news_content(self.store, 1, settings), before)
        self.assertIn('переносы', before)
        self.assertIn('вручную', before)
        self.assertNotIn('18:30', before)
        self.assertNotIn('Новый заголовок', before)
        self.h.channels[11].send.assert_not_awaited()
        news_card.edit.assert_not_awaited()
        manual.edit.assert_not_awaited()
        self.assertEqual(manual.content, 'Встречу перенесли на воскресенье, начало в 16:00.')

    async def test_chat_intro_describes_flood_and_stays_single(self):
        await self.service.refresh(self.h.guild)
        pub = self.store.publication('chat:1')
        intro = self.h.channels[12].messages[pub['message_id']]
        self.assertIn('флуда', intro.content)
        self.assertIn('свободного общения', intro.content)
        self.assertIn('<#11>', intro.content)
        self.h.channels[12].send.reset_mock()
        await self.service.refresh(self.h.guild)
        self.h.channels[12].send.assert_not_awaited()
        intro.edit.assert_not_awaited()

    async def test_catalog_publication_contains_working_persistent_buttons(self):
        await self.service.refresh(self.h.guild)
        calls = self.h.channels[13].create_thread.await_args_list
        call = next(call for call in calls if call.kwargs['name'] == 'Каталог книжного клуба')
        view = call.kwargs['view']
        self.assertIsInstance(view, CatalogView)
        self.assertTrue(view.is_persistent())
        self.assertEqual([button.label for button in view.children], ['Добавить книгу', 'Загрузить список', 'Порядок чтения', 'Удалённые книги'])
        pub = self.store.publication('catalog:1')
        content = self.h.channels[pub['channel_id']].messages[pub['message_id']].content
        self.assertIn('Сейчас читаем', content)

    async def test_books_command_shows_catalog_buttons_to_read_only_member(self):
        self.h.channels[13].permissions_for.return_value.send_messages = False
        ctx = SimpleNamespace(guild=self.h.guild, author=self.h.members[1])
        self.cog.say = AsyncMock()
        await self.cog.books.callback(self.cog, ctx)
        self.assertIsInstance(self.cog.say.await_args_list[0].kwargs['view'], CatalogView)

    async def test_cog_load_registers_catalog_buttons_for_restart(self):
        with patch.object(type(self.cog.worker), 'start'):
            await self.cog.cog_load()
        views = [call.args[0] for call in self.h.bot.add_view.call_args_list]
        catalogs = [view for view in views if isinstance(view, CatalogView)]
        self.assertEqual(len(catalogs), 1)
        self.assertEqual(catalogs[0].guild_id, 1)

    async def test_library_import_is_attachment_subcommand_within_discord_limit(self):
        async with commands.Bot(command_prefix='!', intents=discord.Intents.default()) as bot:
            cog = Club(bot, self.store)
            await bot.add_cog(cog)
            try:
                payload = bot.tree.get_command('club').to_dict(bot.tree)
                self.assertEqual(len(payload['options']), 25)
                library = next(option for option in payload['options'] if option['name'] == 'library')
                self.assertEqual(library['type'], discord.AppCommandOptionType.subcommand_group.value)
                command = next(option for option in library['options'] if option['name'] == 'import')
                file = next(option for option in command['options'] if option['name'] == 'file')
                self.assertEqual(file['type'], discord.AppCommandOptionType.attachment.value)
                self.assertTrue(file['required'])
            finally:
                await bot.remove_cog('Club')


class SetupCustomizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'club.db'
        self.store = Store(self.path)
        self.h = SetupHarness()
        self.service = self.make_service()

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def make_service(self):
        service = Service(self.h.bot, self.store)
        service.refresh = AsyncMock()
        service.essay_webhook = AsyncMock()
        return service

    async def setup(self, **kwargs):
        return await self.service.setup_server(self.h.guild, 99, **kwargs)

    @staticmethod
    def humans(channel, bot_id):
        return {target.id: tuple(value.value for value in overwrite.pair())
                for target, overwrite in channel.overwrites.items() if target.id != bot_id}

    async def test_native_channel_names_and_topics_survive_repeated_setup(self):
        await self.setup()
        settings = self.store.settings(1)
        names = {'news': 'объявления', 'chat': 'гостиная', 'books': 'полка', 'essays': 'читательские-эссе', 'voice': 'Зал'}
        for purpose, name in names.items():
            channel = self.h.channels[settings[purpose]]
            channel.name = name
            if purpose in ('news', 'chat'):
                channel.topic = 'Наше собственное описание'
        self.h.clear_writes()
        await self.setup()
        self.assertEqual(self.h.create_count(), 0)
        for purpose, name in names.items():
            self.assertEqual(self.store.settings(1)[purpose], settings[purpose])
            self.assertEqual(self.h.channels[settings[purpose]].name, name)
            self.assertEqual(self.store.setup_resource(1, purpose)['name'], name)
            if purpose in ('news', 'chat'):
                self.assertEqual(self.h.channels[settings[purpose]].topic, 'Наше собственное описание')

    async def test_saved_native_channel_name_survives_recreation_after_restart(self):
        await self.setup()
        books_id = self.store.settings(1)['books']
        self.h.channels[books_id].name = 'наша-книжная-полка'
        await self.setup()
        del self.h.channels[books_id]
        self.store = Store(self.path)
        self.service = self.make_service()
        await self.setup()
        replacement_id = self.store.settings(1)['books']
        self.assertNotEqual(replacement_id, books_id)
        self.assertEqual(self.h.channels[replacement_id].name, 'наша-книжная-полка')

    async def test_default_setup_reports_bot_rights_without_changing_any_acl(self):
        await self.setup()
        settings = self.store.settings(1)
        channel = self.h.channels[settings['chat']]
        channel.overwrites[self.h.bot_member].send_messages = False
        before = {ident: self.humans(item, self.h.bot_member.id) for ident, item in self.h.channels.items()}
        self.h.clear_writes()
        report = await self.setup()
        self.assertIn('repair_permissions:true', '\n'.join(report))
        self.assertFalse(channel.permissions_for(self.h.bot_member).send_messages)
        for ident, item in self.h.channels.items():
            item.set_permissions.assert_not_awaited()
            self.assertEqual(self.humans(item, self.h.bot_member.id), before[ident])

    async def test_explicit_bot_repair_preserves_all_human_permissions(self):
        await self.setup()
        channel = self.h.channels[self.store.settings(1)['chat']]
        channel.overwrites[self.h.members[2]] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        channel.overwrites[self.h.bot_member].send_messages = False
        before = self.humans(channel, self.h.bot_member.id)
        self.h.clear_writes()
        await self.setup(repair_permissions=True)
        self.assertTrue(channel.permissions_for(self.h.bot_member).send_messages)
        self.assertEqual(self.humans(channel, self.h.bot_member.id), before)
        channel.set_permissions.assert_awaited_once()
        self.assertIs(channel.set_permissions.await_args.args[0], self.h.bot_member)

    async def test_new_children_copy_manually_restricted_category_permissions(self):
        category = self.h.make_channel(500, discord.CategoryChannel, name='Закрытая библиотека', overwrites={
            self.h.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False,
                                                             send_messages_in_threads=False, connect=False),
            self.h.organizer_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True),
            self.h.members[2]: discord.PermissionOverwrite(view_channel=True, send_messages=False, connect=False),
            self.h.bot_member: discord.PermissionOverwrite(view_channel=True, read_message_history=True),
        })
        before = self.humans(category, self.h.bot_member.id)
        await self.setup(category=category)
        settings = self.store.settings(1)
        for purpose in ('news', 'chat', 'books', 'essays', 'voice'):
            channel = self.h.channels[settings[purpose]]
            self.assertEqual(channel.category_id, category.id)
            self.assertEqual(self.humans(channel, self.h.bot_member.id), before)
        category.set_permissions.assert_not_awaited()

    async def test_new_topics_explain_channel_purposes_and_legacy_bot_topics_upgrade(self):
        await self.setup()
        settings = self.store.settings(1)
        self.assertIn('переносы', self.h.channels[settings['news']].topic)
        self.assertIn('вручную', self.h.channels[settings['news']].topic)
        self.assertIn('флуда', self.h.channels[settings['chat']].topic)
        for purpose in ('news', 'chat'):
            resource = self.store.setup_resource(1, purpose)
            self.h.channels[settings[purpose]].topic = OLD_DESCRIPTIONS[purpose] + '\n' + Provisioner.marker(resource)
        await self.setup()
        for purpose in ('news', 'chat'):
            self.assertTrue(self.h.channels[settings[purpose]].topic.startswith(DESCRIPTIONS[purpose]))


if __name__ == '__main__':
    unittest.main()
