"""Club presentation, queue mutations and rules use disposable persistent state."""
import json
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock

from bookclub.club_format import get_format, save_format, reading_count, format_content
from bookclub.format_controls import FormatView, FormatModal
from bookclub.queue_controls import move_book, queue_books, open_queue
from bookclub.render import book_pages, catalog_pages, material_links
from bookclub.store import ClubError, Store
from bookclub.ui import Club
from test_bookclub import ClubFixture
from test_webhook_discord import WebhookHarness


class PresentationStoreTests(ClubFixture, unittest.TestCase):
    def test_default_plan_and_clean_book_card_with_masked_material_link(self):
        self.assertEqual(reading_count(self.book), 3)
        self.store.update_book(1, self.book['id'], materials='https://example.org/Книга%20(1).pdf')
        book = self.store.book(1, self.book['id'])
        text = '\n'.join(book_pages(self.store, book, self.store.settings(1)))
        self.assertIn('3 встречи по книге + обсуждение эссе', text)
        self.assertIn('[Читать PDF](<https://example.org/', text)
        self.assertIn('%281%29.pdf>', text)
        self.assertIn('https://discord.com/events/1/101', text)
        for unwanted in ('bc:', 'Срок эссе', 'Камеры', 'Участники чтения', 'План встреч пока не задан'):
            self.assertNotIn(unwanted, text)
        self.assertEqual(book['materials'], 'https://example.org/Книга%20(1).pdf')

    def test_materials_preserve_authored_markdown_labels_and_epub_queries(self):
        source = '[Мой PDF](https://example.org/file.pdf)\nhttps://example.org/file.epub?download=1'
        self.assertEqual(material_links(source), '[Мой PDF](https://example.org/file.pdf)\n[Скачать EPUB](<https://example.org/file.epub?download=1>)')

    def test_catalog_sections_preserve_queue_and_all_books(self):
        self.store.update_book(1, self.book['id'], status='reading')
        queued = []
        for i in range(12):
            b = self.store.create_book(1, f'Очередь {i:02}', 'Автор', '', str(i))
            self.store.update_book(1, b['id'], status='queued')
            queued.append(b)
        old = self.store.create_book(1, 'Архивная', 'Автор', '', 'old')
        self.store.update_book(1, old['id'], status='read')
        before = self.store.books(1)
        result = catalog_pages(self.store, 1)
        self.assertIn('Сейчас читаем', result[0])
        self.assertIn('Очередь 09', result[0])
        self.assertNotIn('Очередь 10', result[0])
        text = '\n'.join(result)
        self.assertLess(text.index('Очередь 10'), text.index('Архивная'))
        self.assertTrue(all(text.count(b['title']) == 1 for b in queued))
        self.assertEqual(before, self.store.books(1))

    def test_reorder_is_atomic_preserves_archive_and_rejects_stale_forms(self):
        self.store.update_book(1, self.book['id'], status='read')
        archive = self.store.book(1, self.book['id'])
        added = [self.store.create_book(1, name, 'Автор', '', name) for name in ('Первая', 'Вторая')]
        for b in added:
            move_book(self.store, 1, queue_books(self.store, 1), b['id'], 'enqueue', 99)
        snapshot = queue_books(self.store, 1)
        move_book(self.store, 1, snapshot, added[1]['id'], 'up', 99)
        self.assertEqual([b['id'] for b in queue_books(self.store, 1)], [added[1]['id'], added[0]['id']])
        with self.assertRaisesRegex(ClubError, 'изменилась'):
            move_book(self.store, 1, snapshot, added[0]['id'], 'up', 99)
        self.assertEqual(archive, self.store.book(1, archive['id']))

    def test_rules_audit_survives_restart_and_stale_save_fails(self):
        value = get_format(self.store, 1)
        save_format(self.store, 1, 99, 'Новые правила', 36, value['revision'], [101])
        with self.assertRaisesRegex(ClubError, 'уже изменён'):
            save_format(self.store, 1, 99, 'Старое окно', 24, 0)
        restored = Store(self.path, clock=lambda: self.now)
        audit = restored.one('SELECT * FROM bc_format_audit')
        self.assertEqual((audit['actor_id'], audit['old_body'], audit['new_body']), (99, value['body'], 'Новые правила'))
        self.assertEqual(json.loads(audit['new_events']), [101])
        self.assertIn('за 36 ч', format_content(restored, 1))

    def test_deadline_follows_essay_event_moves_cancel_and_rule_changes(self):
        discussion = self.essay_event(self.now + 86400 * 4)
        self.assertEqual(self.store.book(1, self.book['id'])['deadline'], self.now + 86400 * 4)
        old_jobs = self.jobs(self.book)
        self.sync(discussion, start=discussion['start'] + 3600)
        self.assertTrue(all(not self.store.claim_job(j['key']) for j in old_jobs))
        self.assertEqual(self.store.book(1, self.book['id'])['deadline'], self.now + 86400 * 4 + 3600)
        value = get_format(self.store, 1)
        save_format(self.store, 1, 99, value['body'], 48, 0)
        self.assertEqual(self.store.book(1, self.book['id'])['deadline'], self.now + 86400 * 3 + 3600)
        self.sync(discussion, status='cancelled')
        self.assertIsNone(self.store.book(1, self.book['id'])['deadline'])
        self.assertFalse(self.jobs(self.book))

    def test_ambiguous_or_missing_essay_event_pauses_reminders(self):
        self.essay_event(self.now + 86400 * 4)
        extra = self.store.draft_meeting(1, self.book['id'], 'Ещё эссе', 'Эссе', 'Книга', 'extra', plan_kind='essay')
        self.sync(extra, start=self.now + 86400 * 6, event_id=998)
        self.assertFalse(self.jobs(self.book))
        self.sync(extra, status='cancelled')
        self.assertTrue(self.jobs(self.book))
        discussion = self.store.one("SELECT * FROM bc_meetings WHERE request_key='essay-event'")
        self.sync(discussion, status='cancelled', status_confirmed=False)
        self.assertFalse(self.jobs(self.book))

    def test_a_legacy_manual_deadline_cannot_override_discussion(self):
        self.store.update_book(1, self.book['id'], deadline=self.now + 86400 * 9)
        self.assertFalse(self.jobs(self.book))
        self.essay_event(self.now + 86400 * 4)
        self.store.update_book(1, self.book['id'], deadline=self.now + 86400 * 9)
        self.assertEqual(self.store.book(1, self.book['id'])['deadline'], self.now + 86400 * 4)


class PresentationDiscordTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = WebhookHarness()
        self.cog = Club(self.h.bot, self.store)
        self.service = self.cog.service

    async def test_refresh_pins_rules_keeps_ids_and_does_not_edit_again(self):
        self.store.set_published(1)
        self.store.update_book(1, self.book['id'], status='read')
        await self.service.refresh(self.h.guild)
        before = self.store.rows('SELECT key,channel_id,message_id FROM bc_publications ORDER BY key')
        edits = sum(m.edit.await_count for c in self.h.channels.values() if hasattr(c, 'messages') for m in c.messages.values())
        await self.service.refresh(self.h.guild)
        after = self.store.rows('SELECT key,channel_id,message_id FROM bc_publications ORDER BY key')
        self.assertEqual(before, after)
        self.assertEqual(edits, sum(m.edit.await_count for c in self.h.channels.values() if hasattr(c, 'messages') for m in c.messages.values()))
        rule = self.store.publication('format:1')
        message = self.h.channels[rule['channel_id']].messages[rule['message_id']]
        self.assertTrue(message.pinned)
        message.pin.assert_awaited_once()
        self.assertTrue(FormatView(self.service, 1).is_persistent())
        for c in self.h.channels.values():
            if hasattr(c, 'messages'):
                for m in c.messages.values():
                    self.assertNotIn('bc:', m.content)

    async def test_rules_submit_checks_revoked_access_before_writing(self):
        modal = FormatModal(self.service, 1, 99)
        modal.body._value, modal.hours._value, modal.events._value = 'Новые правила', '24', ''
        self.service.actor = AsyncMock(return_value=(self.h.members[99], False))
        with self.assertRaises(ClubError):
            await modal.on_submit(self.h.interaction(99))
        self.assertEqual(get_format(self.store, 1)['revision'], 0)

    async def test_inline_catalog_repairs_deleted_section_link_in_same_refresh(self):
        contents = ['## Сейчас читаем\nКнига', '## Архив\nПрошлые книги', '## Предложения\nНовые книги']
        root = await self.service.paged_post(self.h.guild, 'catalog:1', 13, 'Каталог', contents, inline_first=True)
        old = self.store.publication('catalog:1:page:1')
        thread = self.h.channels[root['channel_id']]
        del thread.messages[old['message_id']]
        # Trigger an edit that discovers deletion after the cached reservation.
        contents[1] += '\nЕщё одна книга'
        await self.service.paged_post(self.h.guild, 'catalog:1', 13, 'Каталог', contents, inline_first=True)
        new = self.store.publication('catalog:1:page:1')
        self.assertNotEqual(new['message_id'], old['message_id'])
        self.assertIn(str(new['message_id']), thread.messages[root['message_id']].content)
        self.assertNotIn(str(old['message_id']), thread.messages[root['message_id']].content)

    async def test_queue_submit_checks_revoked_access_before_writing(self):
        view = await open_queue(self.service, self.h.interaction(99))
        view.selected = self.book['id']
        self.service.actor = AsyncMock(return_value=(self.h.members[99], False))
        with self.assertRaises(ClubError):
            await view.action(self.h.interaction(99), 'enqueue')
        self.assertEqual(self.store.book(1, self.book['id'])['status'], 'proposed')

    async def test_recurring_schedule_follows_discord_without_linking_books(self):
        value = get_format(self.store, 1)
        save_format(self.store, 1, 99, value['body'], 24, 0, [101])
        event = self.h.event(self.meeting['event_id'], name=self.meeting['name'],
                             start_time=datetime.fromtimestamp(self.meeting['start'], timezone.utc))
        self.service.cache_schedule(self.h.guild, [event])
        first = self.store.one('SELECT * FROM bc_schedule_events')
        from datetime import timedelta
        event.start_time += timedelta(days=7)
        self.service.cache_schedule(self.h.guild, [event])
        self.assertEqual(self.store.one('SELECT * FROM bc_schedule_events')['start'], first['start'] + 7 * 86400)
        self.assertEqual(self.store.meeting(1, self.meeting['id'])['start'], self.meeting['start'])
        self.service.cache_schedule(self.h.guild, [])
        self.assertIsNone(self.store.one('SELECT * FROM bc_schedule_events'))
