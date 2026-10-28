"""The reading queue uses stable chronological rows without page messages."""
import asyncio
from types import SimpleNamespace
import unittest

from bookclub.plan_entries import publish_plan
from bookclub.service import Service
from bookclub.store import Store
from bookclub.town_render import legacy_plan_pages, town_sections
from test_bookclub import ClubFixture
from test_bookclub_discord import DiscordHarness


class PlanEntryTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = DiscordHarness()
        self.new_service()
        self.store.set_published(1)
        self.store.update_book(1, self.book['id'], status='queued', position=10)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1
        await self.service.refresh_book(self.h.guild, self.book['id'])
        self.root = await self.service.paged_post(self.h.guild, 'catalog:1:plan', 13,
            'План чтения', town_sections(self.store, 1)['plan'], inline_first=True)

    def new_service(self):
        self.service = Service(self.h.bot, self.store)
        self.addAsyncCleanup(self.service.close)

    def restart(self):
        self.store = Store(self.path, clock=lambda: self.now)
        self.new_service()
        self.root = self.store.publication('catalog:1:plan')

    @staticmethod
    def key(index):
        return f'catalog:1:plan:row:{index}'

    def thread(self):
        return self.h.channels[self.root['channel_id']]

    def row(self, index):
        pub = self.store.publication(self.key(index))
        self.assertIsNotNone(pub, index)
        return pub, self.h.channels[pub['channel_id']].messages[pub['message_id']]

    def managed(self):
        pubs = self.store.rows("SELECT * FROM bc_publications WHERE guild_id=1 AND key LIKE 'catalog:1:plan:row:%'")
        return sorted(pubs, key=lambda pub: int(pub['key'].rsplit(':', 1)[1]))

    def chronological_text(self):
        pubs = sorted(self.managed(), key=lambda pub: pub['message_id'])
        return [self.h.channels[pub['channel_id']].messages[pub['message_id']].content for pub in pubs]

    def history(self):
        return {table: self.store.rows(f'SELECT * FROM {table}') for table in
                ('bc_books', 'bc_essays', 'bc_import_runs', 'bc_import_sources', 'bc_import_budgets')}

    async def another(self, title, *, status='queued', position=20):
        book = self.store.create_book(1, title, 'Другой автор', '', title)
        self.store.update_book(1, book['id'], status=status, position=position)
        await self.service.refresh_book(self.h.guild, book['id'])
        return self.store.book(1, book['id'])

    async def publish(self, *, verify=True):
        return await publish_plan(self.service, self.h.guild, self.root, verify=verify)

    async def test_one_plain_message_per_book_and_no_empty_proposal_page(self):
        other = await self.another('Следующая книга')
        before, root_id = self.history(), self.root['message_id']
        await self.publish()
        self.assertEqual(len(self.managed()), 2)
        for index, book in enumerate((self.book, other), 1):
            _, message = self.row(index)
            card = self.store.publication(f'book:{book["id"]}')
            self.assertIn(book['title'], message.content)
            self.assertIn(book['author'], message.content)
            self.assertIn(f'{index}.', message.content)
            self.assertIn(f'/{card["channel_id"]}/{card["message_id"]}', message.content)
            self.assertEqual(message.components, [])
            self.assertNotIn('##', message.content)
            self.assertNotIn('1/2', message.content)
            self.assertNotIn('bc:', message.content)
            self.assertLessEqual(len(message.content), 2000)
        root = self.thread().messages[root_id]
        self.assertNotIn('1/2', root.content)
        self.assertNotIn('Предложений пока нет', '\n'.join(m.content for m in self.thread().messages.values()))
        self.assertFalse(self.store.rows("SELECT * FROM bc_publications WHERE key LIKE 'catalog:1:plan:page:%'"))
        self.assertEqual(self.history(), before)
        self.assertTrue(all(call.kwargs.get('suppress_embeds') is True for call in self.thread().send.await_args_list))

    async def test_queue_reorder_updates_existing_slots_in_real_message_order(self):
        other = await self.another('Вторая книга')
        await self.publish()
        ids = [pub['message_id'] for pub in self.managed()]
        self.store.update_book(1, other['id'], position=5)
        before = self.history()
        await self.publish()
        self.assertEqual([pub['message_id'] for pub in self.managed()], ids)
        text = self.chronological_text()
        self.assertIn(other['title'], text[0])
        self.assertIn(self.book['title'], text[1])
        self.assertIn('1.', text[0])
        self.assertIn('2.', text[1])
        self.assertEqual(self.thread().send.await_count, 2)
        self.assertEqual(self.history(), before)

    async def test_proposal_promotion_keeps_saved_queue_order_and_same_message_ids(self):
        proposal = await self.another('Предложенная книга', status='proposed', position=5)
        await self.publish()
        ids = [pub['message_id'] for pub in self.managed()]
        self.assertIn('Предложено', self.row(2)[1].content)
        self.store.update_book(1, proposal['id'], status='queued')
        await self.publish()
        self.assertEqual([pub['message_id'] for pub in self.managed()], ids)
        text = self.chronological_text()
        self.assertIn(proposal['title'], text[0])
        self.assertIn(self.book['title'], text[1])
        self.assertNotIn('Предложено', '\n'.join(text))

    async def test_repeat_and_restart_keep_ids_without_new_posts_or_edits(self):
        await self.another('Очередная книга')
        await self.publish()
        ids = [pub['message_id'] for pub in self.managed()]
        writes = {self.row(index)[1].id: self.row(index)[1].edit.await_count for index in (1, 2)}
        before = self.history()
        await self.publish(verify=False)
        self.restart()
        await self.publish()
        self.assertEqual([pub['message_id'] for pub in self.managed()], ids)
        self.assertEqual({self.row(index)[1].id: self.row(index)[1].edit.await_count for index in (1, 2)}, writes)
        self.assertEqual(self.thread().send.await_count, 2)
        self.assertEqual(self.history(), before)

    async def test_lost_send_is_recovered_before_changed_queue_is_published(self):
        thread = self.thread()
        send, sent = thread.send.side_effect, []
        async def send_then_lose(content, **kwargs):
            message = await send(content, **kwargs)
            sent.append(message)
            raise OSError('lost plan row acknowledgement')
        thread.send.side_effect = send_then_lose
        with self.assertRaises(OSError):
            await self.publish()
        self.assertEqual(len(sent), 1)
        self.assertIsNone(self.store.publication(self.key(1))['message_id'])
        self.store.update_book(1, self.book['id'], title='Название после потери ответа')
        other = await self.another('Новая следующая')
        thread.send.side_effect = send
        self.restart()
        await self.publish()
        self.assertEqual(self.row(1)[0]['message_id'], sent[0].id)
        self.assertIn('Название после потери ответа', self.row(1)[1].content)
        self.assertIn(other['title'], self.row(2)[1].content)
        self.assertEqual(thread.send.await_count, 2)
        await self.publish()
        self.assertEqual(thread.send.await_count, 2)

    async def test_lost_send_is_recovered_then_retired_if_queue_becomes_empty(self):
        thread = self.thread()
        send, sent = thread.send.side_effect, []
        async def send_then_lose(content, **kwargs):
            message = await send(content, **kwargs)
            sent.append(message)
            raise OSError('lost plan row acknowledgement')
        thread.send.side_effect = send_then_lose
        with self.assertRaises(OSError):
            await self.publish()
        self.store.update_book(1, self.book['id'], status='read')
        thread.send.side_effect = send
        self.restart()
        before = self.history()
        await self.publish()
        self.assertEqual(self.managed(), [])
        self.assertNotIn(sent[0].id, thread.messages)
        sent[0].delete.assert_awaited_once()
        self.assertEqual(thread.send.await_count, 1)
        self.assertEqual(self.history(), before)

    async def test_lost_edit_reply_then_restart_recovers_same_slot_and_allows_future_updates(self):
        await self.publish()
        pub, message = self.row(1)
        edit = message.edit.side_effect
        failed = False
        async def edit_then_lose(**kwargs):
            nonlocal failed
            result = await edit(**kwargs)
            if not failed:
                failed = True
                raise OSError('lost plan edit reply')
            return result
        message.edit.side_effect = edit_then_lose
        self.store.update_book(1, self.book['id'], title='После первого изменения')
        with self.assertRaises(OSError):
            await self.publish()
        self.assertIn('После первого изменения', message.content)
        self.restart()
        await self.publish()
        self.store.update_book(1, self.book['id'], title='После второго изменения')
        await self.publish()
        self.assertEqual(self.row(1)[0]['message_id'], pub['message_id'])
        self.assertIn('После второго изменения', message.content)
        self.assertEqual(self.thread().send.await_count, 1)

    async def test_queue_shrink_compacts_content_and_deletes_only_unused_tail(self):
        second = await self.another('Вторая очередь', position=20)
        third = await self.another('Третья очередь', position=30)
        await self.publish()
        ids = [pub['message_id'] for pub in self.managed()]
        tail = self.row(3)[1]
        card_ids = {book['id']: self.store.publication(f'book:{book["id"]}')['message_id']
                    for book in (self.book, second, third)}
        self.store.update_book(1, second['id'], status='read')
        before = self.history()
        await self.publish()
        self.assertEqual([pub['message_id'] for pub in self.managed()], ids[:2])
        self.assertIn(self.book['title'], self.row(1)[1].content)
        self.assertIn(third['title'], self.row(2)[1].content)
        self.assertNotIn(tail.id, self.thread().messages)
        tail.delete.assert_awaited_once()
        self.assertNotIn('Раздел обновлён', '\n'.join(m.content for m in self.thread().messages.values()))
        self.assertEqual(self.history(), before)
        self.assertEqual({ident: self.store.publication(f'book:{ident}')['message_id'] for ident in card_ids}, card_ids)

    async def test_fast_compaction_journals_moved_row_before_lost_edit_and_later_queue_change(self):
        second = await self.another('Вторая очередь', position=20)
        third = await self.another('Третья очередь', position=30)
        await self.publish()
        first_id = self.row(1)[0]['message_id']
        removed = self.row(2)[1]
        moved = self.row(3)[1]
        del self.thread().messages[removed.id]
        self.store.update_book(1, second['id'], title='Вторая до потери ответа')
        edit = moved.edit.side_effect
        failed = False

        async def edit_then_lose(**kwargs):
            nonlocal failed
            result = await edit(**kwargs)
            if not failed:
                failed = True
                raise OSError('lost edit reply after fast row compaction')
            return result

        moved.edit.side_effect = edit_then_lose
        with self.assertRaises(OSError):
            await self.publish(verify=False)
        self.assertIn('Вторая до потери ответа', moved.content)
        self.store.update_book(1, second['id'], title='Вторая после потери ответа')
        before = self.history()
        self.restart()
        await self.publish()
        ids = [pub['message_id'] for pub in self.managed()]
        self.assertEqual(ids[:2], [first_id, moved.id])
        self.assertEqual(ids, sorted(ids))
        self.assertIn('Вторая после потери ответа', self.row(2)[1].content)
        self.assertIn(third['title'], self.row(3)[1].content)
        self.assertFalse(self.store.rows("SELECT key FROM bc_publications WHERE key LIKE 'catalog:1:plan:retained:%'"))
        self.assertNotIn('Вторая до потери ответа', '\n'.join(message.content for message in self.thread().messages.values()))
        self.assertEqual(self.history(), before)
        moved.delete.assert_not_awaited()
        sends = self.thread().send.await_count
        await self.publish(verify=False)
        self.assertEqual(self.thread().send.await_count, sends)
        self.assertEqual([pub['message_id'] for pub in self.managed()], ids)

    async def test_missing_middle_row_reindexes_survivors_before_appending_tail(self):
        second = await self.another('Вторая очередь', position=20)
        third = await self.another('Третья очередь', position=30)
        await self.publish()
        first_id, removed_id, last_id = [pub['message_id'] for pub in self.managed()]
        first, last = self.row(1)[1], self.row(3)[1]
        del self.thread().messages[removed_id]
        self.restart()
        before = self.history()
        await self.publish()
        ids = [pub['message_id'] for pub in self.managed()]
        self.assertEqual(ids[:2], [first_id, last_id])
        self.assertGreater(ids[2], last_id)
        self.assertEqual(ids, sorted(ids))
        text = self.chronological_text()
        for expected, actual in zip((self.book['title'], second['title'], third['title']), text):
            self.assertIn(expected, actual)
        first.delete.assert_not_awaited()
        last.delete.assert_not_awaited()
        self.assertEqual(self.thread().send.await_count, 4)
        self.assertEqual(self.history(), before)

    async def test_manual_or_attached_unused_rows_are_retained_without_overwrite(self):
        second = await self.another('Файл в строке', position=20)
        third = await self.another('Заметки в строке', position=30)
        await self.publish()
        attached, manual = self.row(2)[1], self.row(3)[1]
        attached.attachments = [SimpleNamespace(id=77, filename='notes.epub', size=456)]
        manual.content = 'Заметки организатора, сохранённые вручную'
        attached.edit.reset_mock()
        manual.edit.reset_mock()
        for book in (second, third):
            self.store.update_book(1, book['id'], status='read')
        await self.publish()
        for message in (attached, manual):
            self.assertIn(message.id, self.thread().messages)
            message.delete.assert_not_awaited()
            message.edit.assert_not_awaited()
            self.assertIsNotNone(self.store.one('SELECT key FROM bc_publications WHERE message_id=?', (message.id,)))
        self.assertEqual(manual.content, 'Заметки организатора, сохранённые вручную')
        self.assertEqual(attached.attachments[0].filename, 'notes.epub')

    async def test_lost_delete_reply_is_confirmed_without_recreating_the_row(self):
        await self.publish()
        pub, message = self.row(1)
        self.store.update_book(1, self.book['id'], status='read')
        async def delete_then_lose():
            self.assertIsNotNone(self.store.publication(pub['key']))
            del self.thread().messages[message.id]
            raise OSError('lost plan deletion reply')
        message.delete.side_effect = delete_then_lose
        await self.publish()
        self.assertIsNone(self.store.publication(pub['key']))
        self.restart()
        await self.publish()
        self.assertEqual(self.managed(), [])
        self.assertEqual(self.thread().send.await_count, 1)
        message.delete.assert_awaited_once()

    async def test_upgrade_replaces_old_numbered_pages_without_replacing_plan_root(self):
        for index in range(14):
            await self.another(f'Будущая книга {index:02} ' + 'длинное название ' * 7, position=20 + index)
        contents = legacy_plan_pages(self.store, 1)
        self.assertGreater(len(contents), 1)
        self.root = await self.service.paged_post(self.h.guild, 'catalog:1:plan', 13,
            'План чтения', contents, inline_first=True)
        old_root = dict(self.root)
        old_pages = self.store.rows("SELECT * FROM bc_publications WHERE key LIKE 'catalog:1:plan:page:%'")
        old_messages = [self.thread().messages[pub['message_id']] for pub in old_pages]
        before = self.history()
        await self.service.refresh_catalog(self.h.guild, verify=True)
        self.root = self.store.publication('catalog:1:plan')
        self.assertEqual((self.root['channel_id'], self.root['message_id']),
                         (old_root['channel_id'], old_root['message_id']))
        self.assertNotIn('1/', self.thread().messages[self.root['message_id']].content)
        for pub, message in zip(old_pages, old_messages):
            self.assertIsNone(self.store.publication(pub['key']))
            message.delete.assert_awaited_once()
        self.assertEqual(len(self.managed()), 15)
        self.assertEqual(self.history(), before)

    async def test_replacement_plan_preserves_old_topic_history_and_uses_new_rows(self):
        await self.publish()
        old_thread, (_, old_message) = self.thread(), self.row(1)
        before = {ident: message.content for ident, message in old_thread.messages.items()}
        self.store.forget_publication('catalog:1:plan')
        self.root = await self.service.paged_post(self.h.guild, 'catalog:1:plan', 13,
            'План чтения', town_sections(self.store, 1)['plan'], inline_first=True)
        self.assertNotEqual(self.root['channel_id'], old_thread.id)
        await self.publish()
        pub, message = self.row(1)
        self.assertEqual(pub['channel_id'], self.root['channel_id'])
        self.assertNotEqual(message.id, old_message.id)
        self.assertEqual(before, {ident: item.content for ident, item in old_thread.messages.items()})
        old_message.delete.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
