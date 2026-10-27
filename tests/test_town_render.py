"""Public town views use real disposable state, with no Discord or model calls."""
from pathlib import Path
import tempfile
import unittest

from bookclub.store import Store
from bookclub.town_render import pinned_title, town_guide, town_sections


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
        self.assertIn('Текущее чтение', result['current'])
        self.assertIn('Текущее чтение', result['plan'])
        self.assertNotIn('Текущее чтение', result['archive'])
        for title in ('Очередная книга', 'Книга предложение'):
            self.assertIn(title, result['plan'])
            self.assertNotIn(title, result['current'] + result['archive'])
        self.assertIn('Завершённая книга', result['archive'])
        self.assertNotIn('Завершённая книга', result['current'] + result['plan'])
        self.assertNotIn('Удалённый архив', str(result))
        self.assertNotIn('Чужой сервер', str(result))

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
        self.assertIn('4 встречи по книге + обсуждение эссе', result['plan'])
        self.assertIn('Предстоящие встречи по книге ещё не назначены', result['plan'])
        self.store.update_book(1, book['id'], title='Новое название')
        self.assertIn(url, self.text()['current'])
        self.assertIn('Новое название', self.text()['current'])

    def test_status_change_moves_book_from_current_into_archive(self):
        book = self.book('Переход статуса', 'reading')
        self.store.update_book(1, book['id'], status='read')
        result = self.text()
        self.assertNotIn('Переход статуса', result['current'] + result['plan'])
        self.assertIn('Переход статуса', result['archive'])

    def test_queue_uses_saved_positions_and_proposals_stay_separate(self):
        self.book('Вторая очередь', 'queued', position=20)
        self.book('Первая очередь', 'queued', position=10)
        self.book('Не утверждено', 'proposed', position=1)
        text = self.text()['plan']
        self.assertLess(text.index('Первая очередь'), text.index('Вторая очередь'))
        self.assertLess(text.index('Вторая очередь'), text.index('Предложения'))
        self.assertLess(text.index('Предложения'), text.index('Не утверждено'))
        self.assertIn('**1.** [Первая очередь', text)

    def test_long_queue_is_complete_paginated_and_keeps_links_intact(self):
        titles = []
        for index in range(40):
            title = f'Книга {index:03} ' + 'Длинное название ' * 9
            titles.append(title.strip())
            self.book(title, 'queued', author='Автор ' * 25)
        result = town_sections(self.store, 1)
        self.assertGreater(len(result['plan']), 5)
        text = '\n'.join(result['plan'])
        for title in titles:
            self.assertEqual(text.count(title), 1)
        for page in result['plan']:
            self.assertLessEqual(len(page), 1750)
            self.assertTrue(page.startswith('## План чтения'))
            self.assertEqual(page.count('](https://discord.com/channels/'), page.count('https://discord.com/channels/'))
            for line in page.splitlines():
                if 'https://discord.com/channels/' in line:
                    self.assertTrue(line.endswith(')'))
        self.assertIn('**40.**', text)

    def test_only_public_upcoming_meetings_and_actual_schedule_are_shown(self):
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
        self.assertIn('https://discord.com/events/1/401', text)
        self.assertIn('Часть вторая · до главы 7 включительно', text)
        self.assertIn('идёт сейчас', text)
        self.assertIn('Обсуждаем эссе по книге', text)
        self.assertIn('Синк из расписания', text)
        self.assertIn('18.05.2033 06:33 (Europe/Moscow)', text)
        for omitted in ('Отменённая встреча', 'Прошедшая встреча', 'Черновая встреча',
                        'СЕКРЕТНЫЕ ВОПРОСЫ', 'ПРИВАТНЫЕ ЗАМЕТКИ', 'Дубликат события', 'Отменённый синк'):
            self.assertNotIn(omitted, str(result))
        pinned = '\n'.join(town_guide(self.store, 1, {}))
        self.assertNotIn('СЕКРЕТНЫЕ ВОПРОСЫ', pinned)
        self.assertNotIn('ПРИВАТНЫЕ ЗАМЕТКИ', pinned)

    def test_archive_counts_only_submitted_existing_essays(self):
        book = self.book('Прочитано с эссе', 'read')
        self.store.register_essay(1, book['id'], 501, 501, 51, 'Работа', 'https://example.org/one')
        self.store.register_essay(1, book['id'], 502, 502, 52, 'Черновик', 'https://example.org/two', submitted=False)
        self.store.register_essay(1, book['id'], 503, 503, 53, 'Удалено', 'https://example.org/three')
        self.store.delete_essay(1, source_id=503)
        self.assertIn('эссе: 1', self.text()['archive'])

    def test_pin_shows_current_reading_and_two_ordered_links_without_queue_dump(self):
        book = self.book('Текущая книга', 'reading')
        for index in range(10):
            self.book(f'Будущая {index}', 'queued')
        links = {key: f'https://discord.com/channels/1/{index}'
                 for index, key in enumerate(('current', 'plan', 'archive'), 700)}
        result = town_guide(self.store, 1, links)
        self.assertEqual(len(result), 1)
        self.assertLessEqual(len(result[0]), 1750)
        for key in ('plan', 'archive'):
            self.assertIn(links[key], result[0])
        self.assertNotIn(links['current'], result[0])
        for title in ('План чтения', 'Архив прочитанного'):
            self.assertIn(f'[{title}](', result[0])
        self.assertLess(result[0].index('[План чтения]'), result[0].index('[Архив прочитанного]'))
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
        self.assertIn('Опубликовано эссе: **1**', text)
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
        self.assertIn('Опубликовано эссе: **30**', result[0])
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
        self.assertIn('Тема ещё готовится', guide)
        self.assertNotIn('](None)', guide)

    def test_user_text_cannot_ping_or_inject_extra_heading(self):
        self.book('@everyone\n## [Сюрприз]', 'reading')
        result = self.text()['current']
        self.assertNotIn('@everyone', result)
        self.assertNotIn('\n## [Сюрприз]', result)
        self.assertIn('［Сюрприз］', result)


if __name__ == '__main__':
    unittest.main()
