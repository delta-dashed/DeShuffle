"""Public town views use real disposable state, with no Discord or model calls."""
from pathlib import Path
import tempfile
import unittest

from bookclub.store import Store
from bookclub.render import book_pages
from bookclub.town_render import (archive_entries, legacy_archive_pages, legacy_plan_pages,
                                  pinned_title, plan_entries, town_guide, town_sections)


class TownRenderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'town.sqlite3', clock=lambda: 2_000_000_000)
        self.store.configure(1, dict(news=11, chat=12, books=13, essays=14, voice=15,
                                     organizers=[99], organizer_roles=[]))
        self.next_id = 100

    def book(self, title, status='proposed', **fields):
        self.next_id += 1
        book = self.store.create_book(1, title, 'Автор', '', str(self.next_id))
        self.store.update_book(1, book['id'], status=status, **fields)
        key = f'book:{book["id"]}'
        self.store.reserve_publication(key, 1, self.next_id)
        self.store.save_publication(key, self.next_id, self.next_id + 1000)
        return self.store.book(1, book['id'])

    def text(self):
        return {key: '\n'.join(value) for key, value in town_sections(self.store, 1).items()}

    def meeting(self, book, name, *, status='scheduled', kind='reading', event_id=401):
        meeting = self.store.draft_meeting(1, book['id'], name, 'Часть вторая', '7', name, plan_kind=kind)
        with self.store.tx() as db:
            db.execute('UPDATE bc_meetings SET status=?,event_id=?,start=? WHERE id=?',
                       (status, event_id, 2_000_000_000, meeting['id']))
        return meeting

    def test_empty_topics_have_clear_text_and_nonempty_pages(self):
        result = town_sections(self.store, 1)
        self.assertEqual(set(result), {'current', 'plan', 'archive'})
        for topic in result.values():
            self.assertTrue(topic)
            self.assertTrue(all(0 < len(page) <= 1750 for page in topic))
        self.assertIn('Текущая книга ещё не выбрана', result['current'][0])
        self.assertIn('Очередь пока пуста', result['plan'][0])
        self.assertIn('Прочитанных книг пока нет', result['archive'][0])
        self.assertEqual(archive_entries(self.store, 1), {})

    def test_statuses_deleted_and_foreign_books_are_separated(self):
        self.book('Текущее чтение', 'reading')
        self.book('Очередная книга', 'queued')
        self.book('Книга предложение')
        self.book('Завершённая книга', 'read')
        removed = self.book('Удалённый архив', 'read')
        self.store.remove_book(1, removed['id'], expected_revision=removed['revision'], actor_id=99)
        self.store.configure(2, dict(news=21, chat=22, books=23, essays=24, voice=25))
        self.store.create_book(2, 'Чужой сервер', 'Другой автор', '', 'foreign')
        result = self.text()
        plan = '\n'.join(plan_entries(self.store, 1).values())
        self.assertIn('Текущее чтение', result['current'])
        self.assertNotIn('Текущее чтение', result['plan'])
        self.assertNotIn('Текущее чтение', result['archive'])
        for title in ('Очередная книга', 'Книга предложение'):
            self.assertIn(title, plan)
            self.assertNotIn(title, result['plan'])
            self.assertNotIn(title, result['current'] + result['archive'])
        archived = '\n'.join(archive_entries(self.store, 1).values())
        self.assertIn('Завершённая книга', archived)
        self.assertNotIn('Завершённая книга', result['archive'])
        self.assertNotIn('Завершённая книга', result['current'] + result['plan'])
        self.assertNotIn('Удалённый архив', str(result) + archived)
        self.assertNotIn('Чужой сервер', str(result) + archived + plan)
        self.assertNotIn('Удалённый архив', plan)
        for title in ('Текущее чтение', 'Очередная книга', 'Книга предложение'):
            self.assertNotIn(title, archived)

    def test_current_links_to_persistent_card_without_copying_materials(self):
        book = self.book('Текущая', 'reading', materials='https://example.org/a.pdf', reading_meetings=4)
        # Old databases can retain a deadline that has no confirmed discussion.
        with self.store.tx() as db:
            db.execute('UPDATE bc_books SET deadline=2000000000 WHERE id=?', (book['id'],))
        publication = self.store.publication(f'book:{book["id"]}')
        url = f'https://discord.com/channels/1/{publication["channel_id"]}/{publication["message_id"]}'
        result = self.text()
        self.assertIn(f'[Материалы, встречи и эссе участников]({url})', result['current'])
        self.assertNotIn('https://example.org/a.pdf', result['current'])
        self.assertNotIn('Эссе до:', result['current'])
        self.assertNotIn('18.05.2033', result['current'])
        self.assertNotIn('4 встречи по книге + обсуждение эссе', result['plan'])
        self.assertIn(f'[Встречи и синки — в карточке текущей книги]({url})', result['plan'])
        self.assertIn('4 встречи по книге + обсуждение эссе', '\n'.join(town_guide(self.store, 1, {})))
        self.store.update_book(1, book['id'], title='Новое название')
        self.assertIn(url, self.text()['current'])
        self.assertIn('Новое название', self.text()['current'])

    def test_status_change_moves_book_from_current_into_archive(self):
        book = self.book('Переход статуса', 'reading')
        self.store.update_book(1, book['id'], status='read')
        result = self.text()
        self.assertNotIn('Переход статуса', result['current'] + result['plan'])
        self.assertIn('Переход статуса', archive_entries(self.store, 1)[book['id']])

    def test_queue_uses_saved_positions_and_proposals_stay_separate(self):
        self.book('Вторая очередь', 'queued', position=20)
        self.book('Первая очередь', 'queued', position=10)
        self.book('Не утверждено', 'proposed', position=1)
        before = self.store.books(1)
        entries = plan_entries(self.store, 1)
        text = '\n'.join(entries.values())
        self.assertEqual(list(entries), [1, 2, 3])
        self.assertLess(text.index('Первая очередь'), text.index('Вторая очередь'))
        self.assertLess(text.index('Вторая очередь'), text.index('Предложено'))
        self.assertLess(text.index('Предложено'), text.index('Не утверждено'))
        self.assertIn('1. [Первая очередь', text)
        self.assertEqual(before, self.store.books(1))

    def test_long_queue_is_one_message_per_book_without_pages_and_keeps_links_intact(self):
        titles = []
        for index in range(40):
            title = f'Книга {index:03} ' + 'Длинное название ' * 9
            titles.append(title.strip())
            self.book(title, 'queued', author='Автор ' * 25)
        result = town_sections(self.store, 1)
        self.assertEqual(len(result['plan']), 1)
        entries = plan_entries(self.store, 1)
        self.assertEqual(list(entries), list(range(1, 41)))
        text = '\n'.join(entries.values())
        for title in titles:
            self.assertEqual(text.count(title), 1)
        for page in entries.values():
            self.assertLessEqual(len(page), 1750)
            self.assertNotIn('## ', page)
            self.assertNotIn('\n', page)
            self.assertEqual(page.count('](https://discord.com/channels/'), page.count('https://discord.com/channels/'))
            for line in page.splitlines():
                if 'https://discord.com/channels/' in line:
                    self.assertTrue(line.endswith(')'))
        self.assertIn('40. [', text)
        self.assertNotIn('1/', result['plan'][0])

    def test_plan_empty_proposals_do_not_create_a_section_or_message(self):
        self.book('Только очередь', 'queued')
        intro = town_sections(self.store, 1)['plan']
        self.assertEqual(len(intro), 1)
        self.assertNotIn('Предложени', intro[0])
        entries = plan_entries(self.store, 1)
        self.assertEqual(len(entries), 1)
        self.assertFalse(any('Предложено' in line for line in entries.values()))

    def test_plan_reorder_and_status_changes_follow_current_saved_queue_without_mutation(self):
        first = self.book('Первая', 'queued', position=10)
        second = self.book('Вторая', 'queued', position=20)
        proposed = self.book('Предложение', 'proposed', position=1)
        self.store.update_book(1, second['id'], position=5)
        snapshot = self.store.books(1)
        entries = plan_entries(self.store, 1)
        self.assertIn('1. [Вторая', entries[1])
        self.assertIn('2. [Первая', entries[2])
        self.assertTrue(entries[3].startswith('Предложено · [Предложение'))
        self.assertEqual(snapshot, self.store.books(1))
        self.store.update_book(1, first['id'], status='read')
        self.store.update_book(1, proposed['id'], status='queued', position=7)
        changed = plan_entries(self.store, 1)
        self.assertEqual(list(changed), [1, 2])
        self.assertIn('1. [Вторая', changed[1])
        self.assertIn('2. [Предложение', changed[2])
        self.assertNotIn('Первая', '\n'.join(changed.values()))

    def test_plan_entry_labels_are_safe_and_missing_cards_never_make_fake_links(self):
        book = self.book('@everyone\n[Сюрприз]', 'queued', author='@here\nАвтор')
        entry = plan_entries(self.store, 1)[1]
        self.assertNotIn('@everyone', entry)
        self.assertNotIn('@here', entry)
        self.assertNotIn('\n', entry)
        self.assertIn('［Сюрприз］', entry)
        self.store.forget_publication(f'book:{book["id"]}')
        self.assertNotIn('https://', plan_entries(self.store, 1)[1])
        self.assertNotIn('](None)', plan_entries(self.store, 1)[1])

    def test_legacy_plan_pages_reproduce_old_renderer_without_changing_short_intro(self):
        first = self.book('Очередь', 'queued')
        self.book('Идея', 'proposed')
        first_pub = self.store.publication(f'book:{first["id"]}')
        legacy = legacy_plan_pages(self.store, 1)
        self.assertEqual(legacy, ['## План чтения\n### Очередь чтения\nЧитаем по порядку сверху вниз.\n'
            f'**1.** [Очередь · Автор](https://discord.com/channels/1/{first_pub["channel_id"]}/{first_pub["message_id"]})'
            '\n\n### Предложения · ещё не в очереди\n'
            + next(line.replace('Предложено · ', '• ') for line in plan_entries(self.store, 1).values() if line.startswith('Предложено'))])
        for index in range(25):
            self.book(f'Длинная очередь {index:02} ' + 'Название ' * 12, 'queued')
        legacy = legacy_plan_pages(self.store, 1)
        self.assertGreater(len(legacy), 1)
        self.assertTrue(legacy[0].startswith(f'## План чтения · 1/{len(legacy)}\n'))
        self.assertEqual(len(town_sections(self.store, 1)['plan']), 1)

    def test_plan_omits_meetings_and_syncs_while_current_book_card_retains_them(self):
        book = self.book('План встреч', 'reading')
        first = self.meeting(book, 'Публичная встреча', event_id=401)
        self.meeting(book, 'Текущий разбор', status='active', kind='essay', event_id=402)
        self.meeting(book, 'Отменённая встреча', status='cancelled', event_id=403)
        self.meeting(book, 'Прошедшая встреча', status='completed', event_id=404)
        self.meeting(book, 'Черновая встреча', status='draft', event_id=None)
        with self.store.tx() as db:
            db.execute('INSERT INTO bc_plans(meeting_id,generation,owner_id,topics,notes) VALUES(?,0,99,?,?)',
                       (first['id'], 'СЕКРЕТНЫЕ ВОПРОСЫ', 'ПРИВАТНЫЕ ЗАМЕТКИ'))
            for event_id, name, status in ((401, 'Дубликат события', 'scheduled'),
                                           (410, 'Синк из расписания', 'scheduled'),
                                           (411, 'Отменённый синк', 'cancelled')):
                db.execute('INSERT INTO bc_schedule_events VALUES(1,?,?,?,NULL,15,?)',
                           (event_id, name, 2_000_086_400, status))
        result = self.text()
        text = result['plan']
        for omitted in ('Публичная встреча', 'Текущий разбор', 'Часть вторая', 'до главы 7',
                        'Синк из расписания', 'Ближайшие синки', 'https://discord.com/events/',
                        '18.05.2033', 'План текущей книги', 'Отменённая встреча',
                        'Прошедшая встреча', 'Черновая встреча', 'СЕКРЕТНЫЕ ВОПРОСЫ',
                        'ПРИВАТНЫЕ ЗАМЕТКИ', 'Дубликат события', 'Отменённый синк'):
            self.assertNotIn(omitted, str(result))
        self.assertIn('Встречи и синки — в карточке текущей книги', text)
        card = '\n'.join(book_pages(self.store, book, self.store.settings(1)))
        self.assertIn('https://discord.com/events/1/401', card)
        self.assertIn('Публичная встреча', card)
        self.assertIn('Синк из расписания', card)
        self.assertIn('18.05.2033 06:33 (Europe/Moscow)', card)
        self.assertNotIn('СЕКРЕТНЫЕ ВОПРОСЫ', card)
        self.assertNotIn('ПРИВАТНЫЕ ЗАМЕТКИ', card)
        pinned = '\n'.join(town_guide(self.store, 1, {}))
        self.assertNotIn('СЕКРЕТНЫЕ ВОПРОСЫ', pinned)
        self.assertNotIn('ПРИВАТНЫЕ ЗАМЕТКИ', pinned)

    def test_archive_counts_only_submitted_existing_essays(self):
        book = self.book('Прочитано с эссе', 'read')
        self.store.register_essay(1, book['id'], 501, 501, 51, 'Работа', 'https://example.org/one')
        self.store.register_essay(1, book['id'], 502, 502, 52, 'Черновик', 'https://example.org/two', submitted=False)
        self.store.register_essay(1, book['id'], 503, 503, 53, 'Удалено', 'https://example.org/three')
        self.store.delete_essay(1, source_id=503)
        self.assertIn('эссе: 1', archive_entries(self.store, 1)[book['id']])

    def test_archive_is_one_short_introduction_and_one_complete_message_per_book(self):
        books = []
        for index in range(45):
            books.append(self.book(f'Прочитанная {index:02} ' + 'Длинное название ' * 7,
                                   'read', author='Имя автора ' * 12, position=100 - index))
        ordered = [book['id'] for book in self.store.books(1) if book['status'] == 'read']
        before = self.store.books(1)
        intro = town_sections(self.store, 1)['archive']
        self.assertEqual(len(intro), 1)
        self.assertEqual(intro[0], '## Архив прочитанного\nПрочитанные книги, их материалы и эссе участников.')
        entries = archive_entries(self.store, 1)
        self.assertEqual(list(entries), ordered)
        self.assertEqual(len(entries), 45)
        for book in books:
            message = entries[book['id']]
            publication = self.store.publication(f'book:{book["id"]}')
            self.assertIn(' '.join(book['title'].split()), message)
            self.assertIn(' '.join(book['author'].split()), message)
            self.assertIn(f'/{publication["channel_id"]}/{publication["message_id"]}', message)
            self.assertTrue(message.startswith('• ['))
            self.assertTrue(message.endswith('· эссе: 0'))
            self.assertNotIn('\n', message)
            self.assertNotIn('## ', message)
            self.assertLessEqual(len(message), 2000)
        self.assertEqual(before, self.store.books(1))

    def test_archive_entry_follows_metadata_essay_count_and_status_without_changing_key(self):
        book = self.book('Начальное название', 'read')
        original = archive_entries(self.store, 1)
        self.store.update_book(1, book['id'], title='Новое название', author='Другой автор')
        self.store.register_essay(1, book['id'], 501, 501, 51, 'Работа', 'https://example.org/one')
        updated = archive_entries(self.store, 1)
        self.assertEqual(list(updated), list(original))
        self.assertIn('Новое название · Другой автор', updated[book['id']])
        self.assertIn('эссе: 1', updated[book['id']])
        self.assertNotIn('Начальное название', updated[book['id']])
        self.store.update_book(1, book['id'], status='queued')
        self.assertEqual(archive_entries(self.store, 1), {})
        self.assertIn('Прочитанных книг пока нет.', town_sections(self.store, 1)['archive'][0])

    def test_archive_entry_labels_are_single_line_safe_and_missing_cards_have_no_fake_url(self):
        book = self.book('@everyone\n[Сюрприз]', 'read', author='@here\nАвтор')
        entry = archive_entries(self.store, 1)[book['id']]
        self.assertNotIn('@everyone', entry)
        self.assertNotIn('@here', entry)
        self.assertNotIn('\n', entry)
        self.assertIn('［Сюрприз］', entry)
        self.store.forget_publication(f'book:{book["id"]}')
        entry = archive_entries(self.store, 1)[book['id']]
        self.assertNotIn('](None)', entry)
        self.assertNotIn('https://', entry)
        self.assertIn('эссе: 0', entry)

    def test_legacy_archive_pages_reproduce_old_renderer_for_safe_migration(self):
        book = self.book('Прочитанная', 'read')
        publication = self.store.publication(f'book:{book["id"]}')
        url = f'https://discord.com/channels/1/{publication["channel_id"]}/{publication["message_id"]}'
        self.assertEqual(legacy_archive_pages(self.store, 1),
                         ['## Архив прочитанного\nПрочитанные книги, их материалы и эссе участников.\n'
                          f'• [Прочитанная · Автор]({url}) · эссе: 0'])
        for index in range(25):
            self.book(f'Архив {index:02} ' + 'Название ' * 12, 'read')
        legacy = legacy_archive_pages(self.store, 1)
        self.assertGreater(len(legacy), 1)
        self.assertTrue(legacy[0].startswith(f'## Архив прочитанного · 1/{len(legacy)}\n'))
        for entry in archive_entries(self.store, 1).values():
            self.assertIn(entry, '\n'.join(legacy))
        self.assertEqual(len(town_sections(self.store, 1)['archive']), 1)

    def test_pin_shows_current_reading_without_repeating_button_navigation_or_queue(self):
        book = self.book('Текущая книга', 'reading')
        for index in range(10):
            self.book(f'Будущая {index}', 'queued')
        links = {key: f'https://discord.com/channels/1/{index}'
                 for index, key in enumerate(('current', 'plan', 'archive'), 700)}
        result = town_guide(self.store, 1, links)
        self.assertEqual(len(result), 1)
        self.assertLessEqual(len(result[0]), 1750)
        for url in links.values():
            self.assertNotIn(url, result[0])
        self.assertNotIn('План и прочитанное', result[0])
        self.assertIn('Текущая книга', result[0])
        self.assertNotIn('Будущая', result[0])
        publication = self.store.publication(f'book:{book["id"]}')
        self.assertIn(f'/{publication["channel_id"]}/{publication["message_id"]}', result[0])

    def test_pin_contains_named_materials_plan_and_submitted_essay_count(self):
        book = self.book('Нынешняя', 'reading', materials='https://example.org/book.pdf\n[Другое издание](https://example.org/edition)',
                         reading_meetings=5)
        self.store.register_essay(1, book['id'], 501, 501, 51, 'Работа', 'https://example.org/one')
        self.store.register_essay(1, book['id'], 502, 502, 52, 'Черновик', 'https://example.org/two', submitted=False)
        with self.store.tx() as db:
            db.execute('UPDATE bc_books SET deadline=2000000000 WHERE id=?', (book['id'],))
        text = '\n'.join(town_guide(self.store, 1, {}))
        self.assertIn('[Читать PDF](<https://example.org/book.pdf>)', text)
        self.assertIn('[Другое издание](https://example.org/edition)', text)
        self.assertIn('5 встреч по книге + обсуждение эссе', text)
        self.assertIn('[Эссе участников — 1 · Карточка книги]', text)
        self.assertNotIn('Эссе до:', text)
        self.assertNotIn('18.05.2033', text)

    def test_pin_title_and_contents_follow_current_book_and_renaming(self):
        first = self.book('Первая', 'reading')
        second = self.book('Следующая', 'queued')
        self.assertEqual(pinned_title(self.store, 1), 'Текущая книга · Первая')
        self.store.update_book(1, first['id'], title='Уточнённая первая')
        self.assertEqual(pinned_title(self.store, 1), 'Текущая книга · Уточнённая первая')
        self.store.update_book(1, first['id'], status='read')
        self.store.update_book(1, second['id'], status='reading')
        self.assertEqual(pinned_title(self.store, 1), 'Текущая книга · Следующая')
        text = '\n'.join(town_guide(self.store, 1, {}))
        self.assertIn('Следующая', text)
        self.assertNotIn('Уточнённая первая', text)
        current = self.store.book(1, second['id'])
        self.store.remove_book(1, second['id'], expected_revision=current['revision'], actor_id=99)
        self.assertEqual(pinned_title(self.store, 1), 'Текущая книга')
        self.assertIn('Текущая книга ещё не выбрана', '\n'.join(town_guide(self.store, 1, {})))

    def test_pin_title_is_single_line_safe_and_fits_discord(self):
        self.assertEqual(pinned_title(self.store, 1), 'Текущая книга')
        self.book('@everyone\n## [Сюрприз] ' + 'Очень длинное ' * 10, 'reading')
        title = pinned_title(self.store, 1)
        self.assertLessEqual(len(title), 100)
        self.assertNotIn('\n', title)
        self.assertNotIn('@everyone', title)
        self.assertIn('［Сюрприз］', title)

    def test_long_materials_and_many_essays_use_complete_card_link_without_broken_urls(self):
        source = 'https://example.org/' + 'long-path-' * 200 + '.epub'
        book = self.book('Большие материалы', 'reading', materials=source)
        for index in range(30):
            self.store.register_essay(1, book['id'], 500 + index, 500 + index, 50 + index,
                                      'Длинное название эссе ' * 7, f'https://example.org/essay/{index}')
        result = town_guide(self.store, 1, dict(plan='https://example.org/plan', archive='https://example.org/archive'))
        self.assertEqual(len(result), 1)
        self.assertLessEqual(len(result[0]), 1750)
        self.assertIn('[Эссе участников — 30 · Карточка книги]', result[0])
        self.assertIn('Все материалы и файлы — в карточке книги', result[0])
        self.assertNotIn('long-path-', result[0])
        publication = self.store.publication(f'book:{book["id"]}')
        self.assertIn(f'/{publication["channel_id"]}/{publication["message_id"]}', result[0])
        self.assertEqual(self.store.book(1, book['id'])['materials'], source)

    def test_missing_cards_and_section_urls_never_create_fake_links(self):
        book = self.book('Готовится', 'reading')
        self.store.forget_publication(f'book:{book["id"]}')
        self.assertIn('Карточка книги ещё готовится', self.text()['current'])
        guide = '\n'.join(town_guide(self.store, 1, {}))
        self.assertIn('Карточка книги ещё готовится', guide)
        self.assertNotIn('](None)', guide)

    def test_user_text_cannot_ping_or_inject_extra_heading(self):
        self.book('@everyone\n## [Сюрприз]', 'reading')
        result = self.text()['current']
        self.assertNotIn('@everyone', result)
        self.assertNotIn('\n## [Сюрприз]', result)
        self.assertIn('［Сюрприз］', result)


if __name__ == '__main__':
    unittest.main()
