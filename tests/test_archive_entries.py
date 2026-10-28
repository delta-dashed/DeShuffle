"""An archive entry is one stable, recoverable bot message per read book."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from bookclub.archive_entries import publish_archive
from bookclub.service import Service
from bookclub.store import Store
from bookclub.town_render import legacy_archive_pages
from bookclub.ui import Club
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


class ArchiveEntryTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.new_service()
        self.store.set_published(1)
        self.store.update_book(1, self.book['id'], status='read')

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1
        await self.service.refresh_book(self.h.guild, self.book['id'])
        self.root = await self.service.paged_post(self.h.guild, 'catalog:1:archive', 13,
            'Архив прочитанного', ['Прочитанные книги'], inline_first=True)

    def new_service(self):
        self.service = Service(self.h.bot, self.store)
        self.addAsyncCleanup(self.service.close)

    def restart(self):
        self.store = Store(self.path, clock=lambda: self.now)
        self.new_service()
        self.root = self.store.publication('catalog:1:archive')

    @staticmethod
    def key(book):
        return f'catalog:1:archive:book:{book["id"]}'

    def thread(self):
        return self.h.channels[self.root['channel_id']]

    def entry(self, book=None):
        pub = self.store.publication(self.key(book or self.book))
        self.assertIsNotNone(pub)
        return pub, self.h.channels[pub['channel_id']].messages[pub['message_id']]

    async def publish(self, *, verify=True):
        return await publish_archive(self.service, self.h.guild, self.root, verify=verify)

    async def another(self, name, status='read'):
        book = self.store.create_book(1, name, 'Другой автор', '', name)
        self.store.update_book(1, book['id'], status=status)
        await self.service.refresh_book(self.h.guild, book['id'])
        return self.store.book(1, book['id'])

    def history(self):
        return {table: self.store.rows(f'SELECT * FROM {table}') for table in
                ('bc_books', 'bc_essays', 'bc_import_runs', 'bc_import_sources', 'bc_import_budgets')}

    async def test_one_plain_message_per_read_book_without_pagination_or_extra_controls(self):
        another = await self.another('Другая прочитанная')
        queued = await self.another('Будущая книга', 'queued')
        original_root = dict(self.root)
        before = self.history()
        await self.publish()
        for book in (self.book, another):
            pub, message = self.entry(book)
            card = self.store.publication(f'book:{book["id"]}')
            self.assertEqual(pub['channel_id'], self.root['channel_id'])
            self.assertIn(book['title'], message.content)
            self.assertIn(book['author'], message.content)
            self.assertIn(f'/{card["channel_id"]}/{card["message_id"]}', message.content)
            self.assertIn('эссе: 0', message.content)
            self.assertEqual(message.components, [])
            self.assertNotIn('1/2', message.content)
            self.assertNotIn('##', message.content)
            self.assertNotIn('bc:', message.content)
            self.assertLessEqual(len(message.content), 2000)
        self.assertIsNone(self.store.publication(self.key(queued)))
        self.assertEqual(self.root, original_root)
        self.assertEqual(self.history(), before)
        self.assertEqual(self.thread().send.await_count, 2)
        self.assertTrue(all(call.kwargs.get('suppress_embeds') is True
                            for call in self.thread().send.await_args_list))

    async def test_repeat_and_restart_keep_message_ids_and_do_not_send_or_edit_again(self):
        await self.publish()
        pub, message = self.entry()
        before, writes = self.history(), message.edit.await_count
        await self.publish(verify=False)
        self.restart()
        await self.publish()
        self.assertEqual(self.entry()[0]['message_id'], pub['message_id'])
        self.assertEqual(message.edit.await_count, writes)
        self.assertEqual(self.thread().send.await_count, 1)
        self.assertEqual(self.history(), before)

    async def test_rename_and_new_essay_update_same_entry_and_leave_source_records_unchanged(self):
        await self.publish()
        pub, message = self.entry()
        self.store.update_book(1, self.book['id'], title='Исправленное название')
        self.store.register_essay(1, self.book['id'], 50001, 50001, 1, 'Работа',
                                 'https://discord.com/channels/1/50001/50001')
        before = self.history()
        await self.publish()
        self.assertEqual(self.entry()[0]['message_id'], pub['message_id'])
        self.assertIn('Исправленное название', message.content)
        self.assertIn('эссе: 1', message.content)
        self.assertTrue(message.edit.await_args.kwargs['suppress'])
        self.assertEqual(self.thread().send.await_count, 1)
        self.assertEqual(self.history(), before)

    async def test_updated_entry_is_retired_after_real_startup_invalidates_publication_hash(self):
        await self.publish()
        self.store.update_book(1, self.book['id'], title='Новое издание')
        self.store.register_essay(1, self.book['id'], 50002, 50002, 1, 'Эссе',
                                 'https://discord.com/channels/1/50002/50002')
        await self.publish()
        pub, message = self.entry()
        self.store.update_book(1, self.book['id'], status='queued')
        self.store = Store(self.path, clock=lambda: self.now)
        restarted = Club(self.h.bot, self.store)
        self.service = restarted.service
        self.addAsyncCleanup(self.service.close)
        with patch.object(type(restarted.worker), 'start'):
            await restarted.cog_load()
        self.assertIsNone(self.store.publication(pub['key'])['content_hash'])
        before = self.history()
        await self.publish()
        self.assertIsNone(self.store.publication(pub['key']))
        message.delete.assert_awaited_once()
        self.assertEqual(self.thread().send.await_count, 1)
        self.assertEqual(self.history(), before)

    async def test_status_change_removes_only_archive_entry_and_read_again_adds_one_new_entry(self):
        await self.publish()
        pub, message = self.entry()
        card = self.store.publication(f'book:{self.book["id"]}')
        self.store.update_book(1, self.book['id'], status='queued')
        before = self.history()
        await self.publish()
        self.assertIsNone(self.store.publication(pub['key']))
        self.assertNotIn(message.id, self.thread().messages)
        message.delete.assert_awaited_once()
        self.assertEqual(self.store.publication(f'book:{self.book["id"]}'), card)
        self.assertEqual(self.history(), before)
        self.store.update_book(1, self.book['id'], status='read')
        await self.publish()
        new_pub, _ = self.entry()
        self.assertNotEqual(new_pub['message_id'], pub['message_id'])
        self.assertEqual(self.thread().send.await_count, 2)
        await self.publish()
        self.assertEqual(self.thread().send.await_count, 2)

    async def test_deleted_book_disappears_from_archive_without_deleting_its_card_or_essays(self):
        await self.publish()
        _, message = self.entry()
        book = self.store.book(1, self.book['id'])
        self.store.remove_book(1, book['id'], expected_revision=book['revision'], actor_id=99)
        card = self.store.publication(f'book:{book["id"]}')
        before = self.history()
        await self.publish()
        self.assertIsNone(self.store.publication(self.key(book)))
        message.delete.assert_awaited_once()
        self.assertEqual(self.history(), before)
        self.assertEqual(self.store.publication(f'book:{book["id"]}'), card)

    async def test_lost_first_send_response_recovers_original_message_after_restart(self):
        thread = self.thread()
        send, sent = thread.send.side_effect, []
        async def sent_then_lost(content, **kwargs):
            message = await send(content, **kwargs)
            sent.append(message)
            raise OSError('lost archive entry response')
        thread.send.side_effect = sent_then_lost
        with self.assertRaises(OSError):
            await self.publish()
        self.assertEqual(len(sent), 1)
        self.assertIsNone(self.store.publication(self.key(self.book))['message_id'])
        thread.send.side_effect = send
        self.restart()
        await self.publish()
        await self.publish()
        self.assertEqual(self.entry()[0]['message_id'], sent[0].id)
        self.assertEqual(thread.send.await_count, 1)

    async def test_lost_send_recovers_then_removes_entry_if_book_left_archive_before_retry(self):
        thread = self.thread()
        send, sent = thread.send.side_effect, []
        async def sent_then_lost(content, **kwargs):
            message = await send(content, **kwargs)
            sent.append(message)
            raise OSError('lost archive entry response')
        thread.send.side_effect = sent_then_lost
        with self.assertRaises(OSError):
            await self.publish()
        self.store.update_book(1, self.book['id'], status='queued')
        thread.send.side_effect = send
        self.restart()
        before = self.history()
        await self.publish()
        self.assertIsNone(self.store.publication(self.key(self.book)))
        self.assertNotIn(sent[0].id, thread.messages)
        sent[0].delete.assert_awaited_once()
        self.assertEqual(thread.send.await_count, 1)
        self.assertEqual(self.history(), before)

    async def test_manually_changed_or_attached_stale_entries_are_preserved(self):
        another = await self.another('Книга с файлом')
        await self.publish()
        _, custom = self.entry()
        _, attached = self.entry(another)
        custom.content = 'Заметка, которую организатор оставил вручную'
        attached.attachments = [SimpleNamespace(id=55, filename='edition.epub', size=321)]
        for book in (self.book, another):
            self.store.update_book(1, book['id'], status='queued')
        await self.publish()
        custom.delete.assert_not_awaited()
        attached.delete.assert_not_awaited()
        custom.edit.assert_not_awaited()
        attached.edit.assert_not_awaited()
        self.assertEqual(custom.content, 'Заметка, которую организатор оставил вручную')
        self.assertIn(attached.id, self.thread().messages)

    async def test_lost_delete_response_is_confirmed_before_retiring_entry_binding(self):
        await self.publish()
        pub, message = self.entry()
        self.store.update_book(1, self.book['id'], status='queued')
        async def delete_then_lost():
            self.assertIsNotNone(self.store.publication(pub['key']))
            del self.thread().messages[message.id]
            raise OSError('lost delete acknowledgement')
        message.delete.side_effect = delete_then_lost
        await self.publish()
        self.assertIsNone(self.store.publication(pub['key']))
        self.restart()
        await self.publish()
        message.delete.assert_awaited_once()
        self.assertEqual(self.thread().send.await_count, 1)

    async def test_failed_delete_keeps_binding_for_retry_and_never_resends_book(self):
        await self.publish()
        pub, message = self.entry()
        self.store.update_book(1, self.book['id'], status='queued')
        delete = message.delete.side_effect
        message.delete.side_effect = OSError('delete did not reach Discord')
        with self.assertRaises(OSError):
            await self.publish()
        self.assertIsNotNone(self.store.publication(pub['key']))
        self.assertIn(message.id, self.thread().messages)
        message.delete.side_effect = delete
        await self.publish()
        self.assertIsNone(self.store.publication(pub['key']))
        self.assertEqual(self.thread().send.await_count, 1)

    async def test_replacement_archive_topic_keeps_old_history_and_moves_entry_binding(self):
        await self.publish()
        old_root, old_thread = dict(self.root), self.thread()
        old_pub, old_message = self.entry()
        old_content = old_message.content
        self.store.forget_publication('catalog:1:archive')
        self.root = await self.service.paged_post(self.h.guild, 'catalog:1:archive', 13,
            'Архив прочитанного', ['Прочитанные книги'], inline_first=True)
        await self.publish()
        new_pub, _ = self.entry()
        self.assertNotEqual(self.root['channel_id'], old_root['channel_id'])
        self.assertEqual(new_pub['channel_id'], self.root['channel_id'])
        self.assertNotEqual(new_pub['message_id'], old_pub['message_id'])
        self.assertIn(old_message.id, old_thread.messages)
        self.assertEqual(old_message.content, old_content)
        old_message.delete.assert_not_awaited()
        old_message.edit.assert_not_awaited()

    async def test_active_entries_with_manual_text_or_files_survive_later_metadata_changes(self):
        another = await self.another('Прочитанная с файлом')
        await self.publish()
        _, custom = self.entry()
        _, attached = self.entry(another)
        custom.content = 'Личная заметка в архиве'
        attached.attachments = [SimpleNamespace(id=91, filename='reading.epub', size=40)]
        self.store.update_book(1, self.book['id'], title='Новое название')
        self.store.update_book(1, another['id'], title='Уточнение издания')
        await self.publish()
        self.assertEqual(custom.content, 'Личная заметка в архиве')
        self.assertEqual(attached.attachments[0].id, 91)
        custom.edit.assert_not_awaited()
        attached.edit.assert_not_awaited()
        custom.delete.assert_not_awaited()
        attached.delete.assert_not_awaited()

    async def test_lost_edit_acknowledgement_recovers_confirmed_body_before_further_updates(self):
        await self.publish()
        pub, message = self.entry()
        edit = message.edit.side_effect

        async def edited_then_lost(**kwargs):
            await edit(**kwargs)
            raise OSError('archive edit applied but response lost')

        self.store.update_book(1, self.book['id'], title='Название после правки')
        message.edit.side_effect = edited_then_lost
        with self.assertRaises(OSError):
            await self.publish()
        self.assertIn('Название после правки', message.content)
        message.edit.side_effect = edit
        self.restart()
        await self.publish()
        self.assertEqual(self.entry()[0]['message_id'], pub['message_id'])
        self.store.register_essay(1, self.book['id'], 50009, 50009, 1, 'Новая работа',
                                  'https://discord.com/channels/1/50009/50009')
        await self.publish()
        self.assertIn('эссе: 1', message.content)
        self.store.update_book(1, self.book['id'], status='queued')
        await self.publish()
        self.assertIsNone(self.store.publication(pub['key']))
        self.assertEqual(self.thread().send.await_count, 1)

    async def test_lost_edit_then_status_change_retires_confirmed_pending_body_after_restart(self):
        await self.publish()
        pub, message = self.entry()
        edit = message.edit.side_effect

        async def edited_then_lost(**kwargs):
            await edit(**kwargs)
            raise OSError('archive edit applied before status changed')

        self.store.update_book(1, self.book['id'], title='Название до возврата в очередь')
        message.edit.side_effect = edited_then_lost
        with self.assertRaises(OSError):
            await self.publish()
        self.store.update_book(1, self.book['id'], status='queued')
        message.edit.side_effect = edit
        self.restart()
        before = self.history()
        await self.publish()
        self.assertIsNone(self.store.publication(pub['key']))
        self.assertNotIn(message.id, self.thread().messages)
        message.delete.assert_awaited_once()
        self.assertEqual(self.thread().send.await_count, 1)
        self.assertEqual(self.history(), before)

    async def test_archive_deleted_during_guide_refresh_recreates_entries_in_same_pass(self):
        queued = await self.another('Будущая книга', 'queued')
        await self.service.refresh(self.h.guild)
        guide = self.store.publication('catalog:1')
        archive = self.store.publication('catalog:1:archive')
        entry = self.store.publication(self.key(self.book))
        book_card = self.store.publication(f'book:{self.book["id"]}')
        plan = self.store.publication('catalog:1:plan')
        plan_thread = self.h.channels[plan['channel_id']]
        fetch = plan_thread.fetch_message.side_effect
        removed = False

        async def delete_during_plan_fetch(message_id):
            nonlocal removed
            if not removed:
                removed = True
                del self.h.channels[guide['channel_id']]
                del self.h.channels[archive['channel_id']]
            return await fetch(message_id)

        plan_thread.fetch_message.side_effect = delete_during_plan_fetch
        self.store.update_book(1, queued['id'], title='Будущая книга после правки')
        await self.service.refresh_catalog(self.h.guild)
        self.assertTrue(removed)
        new_archive = self.store.publication('catalog:1:archive')
        new_entry = self.store.publication(self.key(self.book))
        self.assertNotEqual(new_archive['channel_id'], archive['channel_id'])
        self.assertEqual(new_entry['channel_id'], new_archive['channel_id'])
        self.assertNotEqual(new_entry['message_id'], entry['message_id'])
        message = self.h.channels[new_entry['channel_id']].messages[new_entry['message_id']]
        self.assertIn(self.book['title'], message.content)
        self.assertIn(f'/{book_card["channel_id"]}/{book_card["message_id"]}', message.content)
        self.assertEqual(self.store.publication(f'book:{self.book["id"]}'), book_card)
        created = self.h.channels[13].create_thread.await_count
        sends = self.h.channels[new_archive['channel_id']].send.await_count
        await self.service.refresh_catalog(self.h.guild)
        self.assertEqual(self.store.publication(self.key(self.book))['message_id'], new_entry['message_id'])
        self.assertEqual(self.h.channels[13].create_thread.await_count, created)
        self.assertEqual(self.h.channels[new_archive['channel_id']].send.await_count, sends)

    async def seed_legacy_pages(self):
        for index in range(12):
            await self.another(f'Длинная книга {index:02} ' + 'прошлое чтение ' * 9)
        contents = legacy_archive_pages(self.store, 1)
        self.assertGreater(len(contents), 1)
        self.root = await self.service.paged_post(self.h.guild, 'catalog:1:archive', 13,
            'Архив прочитанного', contents, inline_first=True)
        return self.store.rows("SELECT * FROM bc_publications WHERE key LIKE 'catalog:1:archive:page:%'")

    async def test_upgrade_replaces_verified_legacy_pages_with_entries_preserving_archive_topic(self):
        old_pages = await self.seed_legacy_pages()
        old_root = dict(self.root)
        messages = [self.thread().messages[pub['message_id']] for pub in old_pages]
        before = self.history()
        await self.service.refresh_catalog(self.h.guild, verify=True)
        root = self.store.publication('catalog:1:archive')
        self.assertEqual((root['channel_id'], root['message_id']),
                         (old_root['channel_id'], old_root['message_id']))
        self.assertNotIn('1/', self.thread().messages[root['message_id']].content)
        for page, message in zip(old_pages, messages):
            self.assertIsNone(self.store.publication(page['key']))
            message.delete.assert_awaited_once()
        read_books = [b for b in self.store.books(1) if b['status'] == 'read']
        entries = self.store.rows("SELECT * FROM bc_publications WHERE key LIKE 'catalog:1:archive:book:%'")
        self.assertEqual(len(entries), len(read_books))
        self.assertEqual(self.history(), before)

    async def test_upgrade_keeps_manual_legacy_page_and_its_file(self):
        old_pages = await self.seed_legacy_pages()
        retained = self.thread().messages[old_pages[0]['message_id']]
        retained.content = 'Дополненные вручную заметки об издании'
        retained.attachments = [SimpleNamespace(id=56, filename='saved.epub', size=456)]
        await self.service.refresh_catalog(self.h.guild, verify=True)
        self.assertIsNotNone(self.store.publication(old_pages[0]['key']))
        self.assertIn(retained.id, self.thread().messages)
        retained.delete.assert_not_awaited()
        self.assertEqual(retained.content, 'Дополненные вручную заметки об издании')
        self.assertEqual(retained.attachments[0].id, 56)


if __name__ == '__main__':
    unittest.main()
