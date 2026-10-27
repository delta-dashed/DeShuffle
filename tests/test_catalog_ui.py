"""Catalog buttons and book imports use mocked Discord and a real SQLite store."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bookclub.catalog_ui import (AddBookModal, BookListModal, CatalogView,
                                  catalog_access, preview_book_file, preview_book_text)
from bookclub.service import Service
from bookclub.store import ClubError, Store
from test_bookclub import stub_delivery_transport, CONFIG
from test_bookclub_discord import DiscordHarness


class CatalogUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stub_delivery_transport(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'club.db')
        self.store.configure(1, {**CONFIG, 'published': True})
        self.store.set_published(1)
        self.h = DiscordHarness()
        self.service = Service(self.h.bot, self.store)

    async def asyncSetUp(self):
        import asyncio
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def modal(self):
        modal = AddBookModal(self.service, 1)
        modal.book_title._value = 'Дюна'
        modal.author._value = 'Фрэнк Герберт'
        modal.materials._value = 'https://example.org/book'
        return modal

    def attachment(self, raw=b'Title | Author', *, filename='books.txt', size=None):
        return SimpleNamespace(filename=filename, size=len(raw) if size is None else size,
                               read=AsyncMock(return_value=raw))

    async def test_persistent_catalog_buttons_open_modals_without_http(self):
        view = CatalogView(self.service, 1)
        self.assertTrue(view.is_persistent())
        self.assertEqual([c.custom_id for c in view.children], ['bc:catalog:1:add', 'bc:catalog:1:import', 'bc:catalog:1:queue', 'bc:catalog:1:deleted'])
        interaction = self.h.interaction(99)
        await view.children[0].callback(interaction)
        self.assertIsInstance(interaction.response.send_modal.await_args.args[0], AddBookModal)
        await view.children[1].callback(interaction)
        self.assertIsInstance(interaction.response.send_modal.await_args.args[0], BookListModal)
        self.h.guild.fetch_member.assert_not_awaited()
        self.h.bot.fetch_channel.assert_not_awaited()

    async def test_catalog_buttons_reject_other_guild(self):
        interaction = self.h.interaction(99)
        interaction.guild_id = 2
        with self.assertRaises(ClubError):
            await CatalogView(self.service, 1).children[0].callback(interaction)
        interaction.response.send_modal.assert_not_awaited()

    async def test_member_cannot_open_bulk_import(self):
        interaction = self.h.interaction(1)
        with self.assertRaises(ClubError):
            await CatalogView(self.service, 1).children[1].callback(interaction)
        interaction.response.send_modal.assert_not_awaited()

    async def test_single_add_creates_book_topic_and_links_it(self):
        interaction = self.h.interaction(1)
        await self.modal().on_submit(interaction)
        books = self.store.books(1)
        self.assertEqual(len(books), 1)
        publication = self.store.publication(f'book:{books[0]["id"]}')
        self.assertTrue(publication['message_id'])
        self.assertEqual(self.h.channels[publication['channel_id']].name, 'Дюна · Фрэнк Герберт')
        message = interaction.followup.send.await_args
        self.assertTrue(message.kwargs['ephemeral'])
        self.assertIn(f'/{publication["channel_id"]}/{publication["message_id"]}', message.args[0])
        self.h.guild.fetch_member.assert_awaited_with(1)
        self.h.bot.fetch_channel.assert_any_await(13)

    async def test_same_modal_delivery_is_idempotent(self):
        interaction = self.h.interaction()
        modal = self.modal()
        await modal.on_submit(interaction)
        threads = self.h.channels[13].create_thread.await_count
        await modal.on_submit(interaction)
        self.assertEqual(len(self.store.books(1)), 1)
        self.assertEqual(self.h.channels[13].create_thread.await_count, threads)

    async def test_manual_forum_permission_denies_single_book(self):
        self.h.channels[13].permissions_for.return_value.send_messages = False
        with self.assertRaises(ClubError):
            await self.modal().on_submit(self.h.interaction())
        self.assertEqual(self.store.books(1), [])
        self.h.channels[13].create_thread.assert_not_awaited()

    async def test_preview_does_not_write_and_confirm_deduplicates(self):
        interaction = self.h.interaction(99)
        preview = await preview_book_text(interaction, self.service, 'Дюна | Фрэнк Герберт\nДюна | Фрэнк Герберт\n1984 | Джордж Оруэлл')
        self.assertEqual(self.store.books(1), [])
        self.assertIn('3 книг', interaction.followup.send.await_args.args[0])
        await preview.confirm.callback(interaction)
        self.assertEqual(len(self.store.books(1)), 2)
        self.assertTrue(preview.completed)
        self.assertIn('Пропущено повторов: 1', interaction.followup.send.await_args.args[0])

    async def test_confirmation_rechecks_organizer_and_forum_permissions(self):
        interaction = self.h.interaction(99)
        preview = await preview_book_text(interaction, self.service, 'Дюна | Фрэнк Герберт')
        self.h.channels[13].permissions_for.return_value.view_channel = False
        with self.assertRaises(ClubError):
            await preview.confirm.callback(interaction)
        self.assertEqual(self.store.books(1), [])
        self.assertFalse(preview.completed)

    async def test_removed_organizer_cannot_confirm(self):
        interaction = self.h.interaction(99)
        preview = await preview_book_text(interaction, self.service, 'Дюна | Фрэнк Герберт')
        self.h.guild.owner_id = 90
        self.h.members[99].roles = []
        self.store.configure(1, {**CONFIG, 'organizers': [], 'organizer_roles': []})
        with self.assertRaises(ClubError):
            await preview.confirm.callback(interaction)
        self.assertEqual(self.store.books(1), [])

    async def test_other_user_cannot_confirm_preview(self):
        preview = await preview_book_text(self.h.interaction(99), self.service, 'Дюна | Фрэнк Герберт')
        with self.assertRaises(ClubError):
            await preview.confirm.callback(self.h.interaction(1))
        self.assertEqual(self.store.books(1), [])

    async def test_failed_refresh_can_retry_without_duplicate_books(self):
        interaction = self.h.interaction(99)
        preview = await preview_book_text(interaction, self.service, 'Дюна | Фрэнк Герберт')
        self.service.refresh = AsyncMock(side_effect=[RuntimeError('Discord unavailable'), None])
        with self.assertRaises(RuntimeError):
            await preview.confirm.callback(interaction)
        self.assertFalse(preview.completed)
        self.assertEqual(len(self.store.books(1)), 1)
        await preview.confirm.callback(interaction)
        self.assertTrue(preview.completed)
        self.assertEqual(len(self.store.books(1)), 1)
        self.assertEqual(self.service.refresh.await_count, 2)

    async def test_file_csv_is_only_previewed(self):
        attachment = self.attachment('название,автор\nДюна,Фрэнк Герберт'.encode('utf-8-sig'), filename='books.csv')
        preview = await preview_book_file(self.h.interaction(99), self.service, attachment)
        self.assertEqual(preview.books[0]['title'], 'Дюна')
        self.assertEqual(self.store.books(1), [])
        attachment.read.assert_awaited_once()

    async def test_unauthorized_file_is_not_downloaded(self):
        attachment = self.attachment()
        with self.assertRaises(ClubError):
            await preview_book_file(self.h.interaction(1), self.service, attachment)
        attachment.read.assert_not_awaited()

    async def test_oversized_file_is_not_downloaded(self):
        attachment = self.attachment(size=65537)
        with self.assertRaises(ClubError):
            await preview_book_file(self.h.interaction(99), self.service, attachment)
        attachment.read.assert_not_awaited()

    async def test_downloaded_size_is_verified(self):
        attachment = self.attachment(b'a' * 65537, size=100)
        with self.assertRaises(ClubError):
            await preview_book_file(self.h.interaction(99), self.service, attachment)
        self.assertEqual(self.store.books(1), [])

    async def test_invalid_encoding_is_rejected_without_writes(self):
        attachment = self.attachment(b'\xff\xfe')
        with self.assertRaisesRegex(ClubError, 'UTF-8'):
            await preview_book_file(self.h.interaction(99), self.service, attachment)
        self.assertEqual(self.store.books(1), [])

    async def test_large_preview_pages_are_private_and_bounded(self):
        interaction = self.h.interaction(99)
        interaction.edit_original_response = AsyncMock()
        text = '\n'.join(f'{number} книга с достаточно длинным названием | Длинное имя автора'
                         for number in range(1, 51))
        preview = await preview_book_text(interaction, self.service, text)
        self.assertGreater(len(preview.preview_pages), 1)
        self.assertLessEqual(len(preview.content()), 2000)
        await preview.next_page.callback(interaction)
        self.assertEqual(preview.index, 1)
        self.assertIn('Страница 2/', interaction.edit_original_response.await_args.kwargs['content'])
        self.assertEqual(interaction.edit_original_response.await_args.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_read_only_forum_allows_listing_but_not_creation(self):
        self.h.channels[13].permissions_for.return_value.send_messages = False
        forum = await catalog_access(self.service, self.h.guild, 1, write=False)
        self.assertEqual(forum.id, 13)
        with self.assertRaises(ClubError):
            await catalog_access(self.service, self.h.guild, 1)


if __name__ == '__main__':
    unittest.main()
