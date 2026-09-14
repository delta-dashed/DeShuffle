"""Published essay navigation uses real disposable SQLite state."""
import unittest

from bookclub.render import book_pages, essay_lines, meeting_lines, pages
from test_bookclub import ClubFixture


class EssayRenderTests(ClubFixture, unittest.TestCase):
    def add_essay(self, source_id, author_id, *, submitted=1, managed=0):
        url = f'https://discord.com/channels/1/{source_id}/{source_id}'
        self.store.register_essay(1, self.book['id'], source_id, source_id, author_id,
                                  f'Работа {source_id}', url, managed=managed, submitted=submitted)
        return url

    def test_draft_and_deleted_posts_do_not_appear_as_published_essays(self):
        draft_url = self.add_essay(700, 1, managed=1, submitted=0)
        deleted_url = self.add_essay(701, 2)
        self.store.delete_essay(1, source_id=701)
        content = '\n'.join(essay_lines(self.store, self.book))
        self.assertIn('Пока нет опубликованных работ.', content)
        self.assertNotIn(draft_url, content)
        self.assertNotIn(deleted_url, content)
        self.store.register_essay(1, self.book['id'], 700, 700, 1, 'Работа автора', draft_url,
                                  submitted=1)
        content = '\n'.join(essay_lines(self.store, self.book))
        self.assertIn(f'<@1> · [Работа автора]({draft_url})', content)
        self.assertNotIn('Пока нет опубликованных работ.', content)

    def test_all_authors_and_multiple_works_survive_large_paginated_cards(self):
        urls = [self.add_essay(800 + index, index // 2 + 1) for index in range(90)]
        self.store.update_book(1, self.book['id'], materials='Материалы книги. ' * 220)
        book = self.store.book(1, self.book['id'])
        settings = self.store.settings(1)
        card_pages = book_pages(self.store, book, settings)
        button_pages = pages(essay_lines(self.store, book))
        self.assertGreater(len(card_pages), 2)
        self.assertTrue(all(len(page) <= 1750 for page in card_pages + button_pages))
        card, button = '\n'.join(card_pages), '\n'.join(button_pages)
        for url in urls:
            self.assertEqual(card.count(url), 1)
            self.assertEqual(button.count(url), 1)
            self.assertLess(card.index(url), card.index('Материалы:'))
            self.assertLess(card.index(url), card.index('**Встречи**'))
        self.assertIn('<@45>', card)
        self.assertIn('«Добавить своё эссе»', card_pages[0])
        self.assertIn('«Эссе участников»', card_pages[0])

    def test_meeting_links_to_published_book_and_essay_navigation(self):
        settings = self.store.settings(1)
        self.assertNotIn('Книга и эссе участников', '\n'.join(meeting_lines(self.store, self.meeting, settings)))
        key = f'book:{self.book["id"]}'
        self.store.reserve_publication(key, 1, 13)
        self.store.save_publication(key, 910, 910, 'hash')
        content = '\n'.join(meeting_lines(self.store, self.meeting, settings))
        self.assertIn('[Книга и эссе участников](https://discord.com/channels/1/910/910)', content)
        self.assertIn('Часть 1; до главы 5 · Возвращение включительно.', content)


if __name__ == '__main__':
    unittest.main()
