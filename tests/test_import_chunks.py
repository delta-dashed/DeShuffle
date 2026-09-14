"""Readable new archive parts and durable compatibility with released layouts."""
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from bookclub.archive_import import ArchiveImporter, validate_plan
from bookclub.import_chunks import archive_body, archive_chunks, publication_chunks
from bookclub.import_config import ImportConfig
from bookclub.import_publication import matches_clean_content
from bookclub.service import Service
from bookclub.store import ClubError, Store
from test_archive_import import ArchiveHarness, confirm_book_thread
from test_bookclub import ClubFixture


def snapshot(content, attachments=()):
    return {'id': '61', 'content': content, 'attachments': list(attachments)}


class ArchiveChunkBoundaryTests(unittest.TestCase):
    def test_words_keep_exact_separators_and_do_not_split_in_the_middle(self):
        body = 'Размышления  о\tкниге, её героях и последствиях. ' * 110
        chunks = archive_chunks(snapshot(body))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(chunks), body)
        self.assertTrue(all(0 < len(chunk) <= 1500 for chunk in chunks))
        self.assertTrue(all(chunk[-1].isspace() for chunk in chunks[:-1]))
        self.assertTrue(all(matches_clean_content(chunk.strip(' \t'), chunk) for chunk in chunks))

    def test_paragraph_boundary_is_preferred_to_a_later_word_boundary(self):
        paragraph = 'Первый абзац. ' * 65
        following = 'Следующий абзац продолжается. ' * 80
        body = paragraph + '\n\n' + following
        chunks = archive_chunks(snapshot(body))
        self.assertEqual(chunks[0], paragraph + '\n\n')
        self.assertEqual(''.join(chunks), body)

    def test_line_boundary_and_crlf_are_preserved(self):
        line = 'Начало эссе с цитатой. ' * 48
        body = line + '\r\n' + 'Продолжение объяснения. ' * 70
        chunks = archive_chunks(snapshot(body))
        self.assertEqual(chunks[0], line + '\r\n')
        self.assertEqual(''.join(chunks), body)

    def test_unicode_nonbreaking_spaces_and_long_unbroken_token_lose_nothing(self):
        for body in ('Книга\u00a0и\u2003эссе 📚. ' * 220, 'Я' * 3101,
                     'Вступление ' + 'Ж' * 3101 + ' окончание', ' \t' + 'А' * 3001):
            with self.subTest(body=body[:30]):
                chunks = archive_chunks(snapshot(body))
                self.assertEqual(''.join(chunks), body)
                self.assertTrue(all(0 < len(chunk) <= 1500 for chunk in chunks))
        self.assertEqual([len(chunk) for chunk in archive_chunks(snapshot('Я' * 3101))], [1500, 1500, 101])

    def test_exact_limit_and_empty_text_are_not_given_extra_parts(self):
        self.assertEqual(archive_chunks(snapshot('Я' * 1500)), ['Я' * 1500])
        self.assertEqual(archive_chunks(snapshot('')), [''])

    def test_legacy_and_released_v2_keep_fixed_boundaries_and_their_captions(self):
        old = snapshot('Развёрнутое эссе. ' * 160,
                       [{'filename': 'essay.txt', 'copy': False}])
        for legacy in (False, True):
            body = archive_body(old, legacy=legacy)
            self.assertEqual(archive_chunks(old, legacy=legacy, fixed=True),
                             [body[index:index + 1500] for index in range(0, len(body), 1500)])
        self.assertIn('см. оригинал', archive_chunks(old, legacy=True)[-1])
        self.assertIn('файл превышает лимит переноса', archive_chunks(old)[-1])
        self.assertNotEqual(archive_chunks(old), archive_chunks(old, fixed=True))


class ArchiveChunkPlanTests(ClubFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.key = 'essay-import:1:61'
        self.old = snapshot('Развёрнутое эссе о книге. ' * 180)

    def parts(self, *, persist=False):
        return publication_chunks(self.store, 1, self.key, self.old, persist=persist)

    def test_preview_does_not_write_publications_plans_or_budget(self):
        before = [self.store.rows(f'SELECT * FROM {table}') for table in
                  ('bc_import_chunk_plans', 'bc_publications', 'bc_import_budgets')]
        self.assertEqual(self.parts(), archive_chunks(self.old))
        after = [self.store.rows(f'SELECT * FROM {table}') for table in
                 ('bc_import_chunk_plans', 'bc_publications', 'bc_import_budgets')]
        self.assertEqual(before, after)

    def test_new_plan_survives_restart_and_future_splitter_change_before_any_send(self):
        chosen = self.parts(persist=True)
        self.assertNotEqual(chosen, archive_chunks(self.old, fixed=True))
        self.assertEqual(self.store.rows('SELECT * FROM bc_publications'), [])
        self.store = Store(self.path, clock=lambda: self.now)
        with patch('bookclub.import_chunks.archive_chunks', side_effect=AssertionError('do not recompute')):
            self.assertEqual(self.parts(), chosen)
            self.assertEqual(self.parts(persist=True), chosen)
        row, = self.store.rows('SELECT * FROM bc_import_chunk_plans')
        self.assertEqual(row['scheme'], 'words-v3')
        self.assertEqual(json.loads(row['chunks']), chosen)

    def test_even_unacknowledged_or_nonfirst_old_part_pins_historical_layout(self):
        key = self.key + ':message:61:1:v2'
        self.store.reserve_publication(key, 1, 80, webhook_id=700)
        self.assertIsNone(self.store.publication(key)['message_id'])
        self.assertEqual(self.parts(persist=True), archive_chunks(self.old, fixed=True))
        row, = self.store.rows('SELECT * FROM bc_import_chunk_plans')
        self.assertEqual(row['scheme'], 'fixed-v2')

    def test_old_bot_copy_also_pins_historical_layout(self):
        self.store.reserve_publication(self.key + ':message:61:0', 1, 80)
        self.assertEqual(self.parts(persist=True), archive_chunks(self.old, fixed=True))

    def test_changed_source_or_damaged_saved_plan_is_rejected(self):
        self.parts(persist=True)
        self.old['content'] += ' ручное изменение'
        with self.assertRaisesRegex(ClubError, 'исходному тексту'):
            self.parts()
        self.old['content'] = self.old['content'].removesuffix(' ручное изменение')
        with self.store.tx() as db:
            db.execute('UPDATE bc_import_chunk_plans SET chunks=?', ('["другой текст"]',))
        with self.assertRaisesRegex(ClubError, 'повреждено'):
            self.parts(persist=True)


class ArchiveChunkResumeTests(ClubFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.h = ArchiveHarness()
        self.h.first.content = 'Развёрнутое эссе о прочитанной книге. ' * 130
        self.h.first.attachments = [SimpleNamespace(
            id=801, filename='essay.txt', size=13,
            to_file=AsyncMock(side_effect=lambda: discord.File(io.BytesIO(b'original-file'), filename='essay.txt')))]
        self.config = ImportConfig(enabled=True, allowed_user_ids=(99,), allowed_guild_ids=(1,),
                                   allowed_channel_ids=(41,), max_runs=1)
        self.runner = SimpleNamespace(analyze=AsyncMock(), login_status=AsyncMock(), close=AsyncMock())
        self.service = Service(self.h.bot, self.store)
        self.importer = ArchiveImporter(self.service, self.config, self.runner)
        confirm_book_thread(self.store, self.book)

    async def test_interrupted_new_apply_and_restyle_keep_saved_parts_ids_and_files_without_model(self):
        captured = await self.importer.capture(self.h.guild, self.h.members[99], self.h.source)
        run = self.importer.ledger.reserve_run(1, 99, 41, self.config.budget_id, 'human-reviewed',
                                               1, 100, 100_000, captured)
        plan = validate_plan(captured, {'essays': [{'book_ref': 'book:' + self.book['id'],
                                                   'message_ids': ['61', '62']}]})
        self.importer.ledger.save_plan(1, run['id'], plan)
        original_upsert = self.importer.publisher.upsert
        first_ids = []

        async def stop_after_first_saved_body(*args, **kwargs):
            copy = await original_upsert(*args, **kwargs)
            first_ids.append(copy['message_id'])
            self.assertTrue(self.store.rows('SELECT * FROM bc_import_chunk_plans WHERE source_id=61'))
            raise OSError('restart after a durable body')

        self.importer.publisher.upsert = stop_after_first_saved_body
        with self.assertRaisesRegex(OSError, 'restart after'):
            await self.importer.apply(self.h.guild, 99, run['id'], confirm=True)
        budget = self.importer.ledger.budget(self.config.budget_id)
        self.store = Store(self.path, clock=lambda: self.now)
        self.service = Service(self.h.bot, self.store)
        self.importer = ArchiveImporter(self.service, self.config, self.runner)
        # A later splitter release must not change the already chosen source 61.
        source = next(item for item in captured['messages'] if item['id'] == '61')
        chosen = publication_chunks(self.store, 1, 'essay-import:1:61', source)
        self.assertNotEqual(chosen, archive_chunks(source, fixed=True))
        await self.importer.apply(self.h.guild, 99, run['id'], confirm=True)
        publications = self.store.rows('SELECT * FROM bc_publications ORDER BY key')
        essay, = self.store.essays(self.book['id'])
        thread = self.h.channels[essay['source_id']]
        self.assertIn(first_ids[0], thread.messages)
        first = thread.messages[first_ids[0]]
        self.assertEqual([(a.filename, a.size) for a in first.attachments], [('essay.txt', 13)])
        self.h.first.attachments[0].to_file.assert_awaited_once()
        ids = set(thread.messages)
        for index, expected in enumerate(chosen):
            copy = self.store.publication(f'essay-import:1:61:message:61:{index}:v2')
            self.assertTrue(matches_clean_content(thread.messages[copy['message_id']].content, expected))
        hook, = self.h.hooks.values()
        hook.send.reset_mock()
        hook.edit_message.reset_mock()
        await self.importer.apply(self.h.guild, 99, run['id'], confirm=True)
        await self.importer.restyle(self.h.guild, 99, run['id'], confirm=False)
        await self.importer.restyle(self.h.guild, 99, run['id'], confirm=True)
        self.assertEqual(set(thread.messages), ids)
        self.assertEqual(self.store.rows('SELECT * FROM bc_publications ORDER BY key'), publications)
        hook.send.assert_not_awaited()
        hook.edit_message.assert_not_awaited()
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id), budget)
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()
