"""Preparation stays private and distinguishes inventory from bounded reading."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bookclub.store import ClubError
from bookclub.ui import Club
from test_import_recovery import RecoveryFixture


class ImportPreparationUITests(RecoveryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.cog = Club(self.h.bot, self.store)
        self.cog.importer = self.importer
        self.ctx = SimpleNamespace(guild=self.h.guild, author=self.h.members[99], channel=self.h.old_thread,
                                   interaction=SimpleNamespace(id=999, followup=SimpleNamespace(send=AsyncMock())))
        self.preparation = SimpleNamespace(inventory=AsyncMock(), select=AsyncMock())
        self.importer.preparation = self.preparation

    def text(self):
        return '\n'.join(call.args[0] for call in self.ctx.interaction.followup.send.await_args_list)

    def assert_private_and_free(self):
        for call in self.ctx.interaction.followup.send.await_args_list:
            self.assertTrue(call.kwargs['ephemeral'])
            self.assertFalse(call.kwargs['allowed_mentions'].everyone)
        self.runner.analyze.assert_not_awaited()
        self.runner.login_status.assert_not_awaited()
        self.assertEqual(self.importer.ledger.budget(self.config.budget_id)['runs_used'], 0)
        self.assertEqual(self.store.rows('SELECT * FROM bc_import_runs'), [])

    def inventory(self, *, complete=True):
        return {'source_id': 41, 'complete': complete, 'warnings': [] if complete else ['Не удалось получить архивные треды.'],
                'threads': [{'id': str(index + 100), 'name': f'Тред {index}', 'archived': index >= 9,
                             'decision': 'excluded' if index == 1 else 'included' if index == 2 else 'pending',
                             'capture_state': 'limited' if index > 9 else 'partial' if index == 2 else 'unread',
                             'title': 'Подтверждённая книга' if index == 2 else None,
                             'author': 'Подтверждённый автор' if index == 2 else None,
                             'book_id': None, 'imported_messages': 6 if index == 0 else 0, 'access': True}
                            for index in range(13)]}

    async def test_inventory_paginates_all_threads_and_attaches_unabridged_inventory(self):
        inventory = self.inventory()
        self.preparation.inventory.return_value = inventory
        await Club.import_inventory.callback(self.cog, self.ctx, page=2)
        text = self.text()
        self.assertIn('13; активных 9, архивных 4', text)
        self.assertIn('Страница 2/2', text)
        self.assertIn('`112`', text)
        self.assertNotIn('`100`', text)
        self.assertIn('не просмотрен из-за лимита', text)
        attachment = self.ctx.interaction.followup.send.await_args.kwargs['file']
        self.assertEqual(json.loads(attachment.fp.getvalue()), inventory)
        self.preparation.inventory.assert_awaited_once_with(self.h.guild, 99, 41)
        self.assert_private_and_free()

    async def test_inventory_distinguishes_imported_excluded_and_partial(self):
        self.preparation.inventory.return_value = self.inventory()
        await Club.import_inventory.callback(self.cog, self.ctx)
        text = self.text()
        self.assertIn('уже импортирован: 6 исходных сообщений', text)
        self.assertIn('исключён', text)
        self.assertIn('просмотрен частично', text)
        self.assertIn('нужен выбор человека', text)
        self.assertIn('Подтверждённый автор', text)
        self.assertIn('page:2', text)
        self.assert_private_and_free()

    async def test_incomplete_inventory_never_claims_full_coverage(self):
        self.preparation.inventory.return_value = self.inventory(complete=False)
        await Club.import_inventory.callback(self.cog, self.ctx)
        self.assertIn('**Неполный перечень', self.text())
        self.assertNotIn('**Полный перечень', self.text())
        self.assertIn('Не удалось получить архивные треды.', self.text())

    async def test_invalid_pages_and_thread_ids_are_rejected(self):
        with self.assertRaises(ClubError):
            await Club.import_inventory.callback(self.cog, self.ctx, page=0)
        self.preparation.inventory.assert_not_awaited()
        self.preparation.inventory.return_value = self.inventory()
        with self.assertRaises(ClubError):
            await Club.import_inventory.callback(self.cog, self.ctx, page=3)
        for thread in ('no', '-1', '0', str(2**63)):
            with self.subTest(thread=thread), self.assertRaises(ClubError):
                await Club.import_prepare.callback(self.cog, self.ctx, thread, 'excluded', confirm=True)
        self.preparation.select.assert_not_awaited()

    async def test_selection_requires_human_confirmation_before_any_write(self):
        with self.assertRaisesRegex(ClubError, 'confirm:true'):
            await Club.import_prepare.callback(self.cog, self.ctx, '51', 'included', title='Название', author='Автор')
        self.preparation.select.assert_not_awaited()
        self.assert_private_and_free()

    async def test_selection_uses_stable_id_and_preserves_explicit_metadata(self):
        self.preparation.select.return_value = {'title': 'Девять миллиардов имён Бога', 'author': 'Артур Кларк'}
        await Club.import_prepare.callback(self.cog, self.ctx, '51', 'included',
                                           title='Девять миллиардов имён Бога', author='Артур Кларк', confirm=True)
        self.preparation.select.assert_awaited_once_with(self.h.guild, 99, 41, 51, 'included',
                                                        title='Девять миллиардов имён Бога', author='Артур Кларк',
                                                        book_id=None, confirm=True)
        self.assertIn('Девять миллиардов имён Бога', self.text())
        self.assertIn('Артур Кларк', self.text())
        self.assert_private_and_free()

    async def test_existing_catalog_mapping_forwards_only_resolved_book(self):
        self.preparation.select.return_value = self.book
        await Club.import_prepare.callback(self.cog, self.ctx, '51', 'included', book=self.book['id'], confirm=True)
        self.preparation.select.assert_awaited_once_with(self.h.guild, 99, 41, 51, 'included',
                                                        title=None, author=None, book_id=self.book['id'], confirm=True)
        self.assert_private_and_free()

    async def test_preparation_prefix_and_unallowlisted_actor_cannot_read_inventory(self):
        self.ctx.interaction = None
        with self.assertRaisesRegex(ClubError, 'slash'):
            await Club.import_inventory.callback(self.cog, self.ctx)
        self.ctx.interaction = SimpleNamespace(id=999)
        self.ctx.author = self.h.members[1]
        with self.assertRaises(ClubError):
            await Club.import_inventory.callback(self.cog, self.ctx)
        self.preparation.inventory.assert_not_awaited()

    async def test_preview_continuation_preserves_whole_queue_from_inside_source_thread(self):
        token = 'a' * 32
        snapshot = {'source_id': 41, 'books': [], 'messages': [], 'warnings': [], 'continuation': 'b' * 32,
                    'coverage': {'complete': False, 'total_threads': 3, 'completed_threads': 1}}
        self.importer.preview = AsyncMock(return_value=snapshot)
        await Club.import_preview.callback(self.cog, self.ctx, cursor=token)
        self.importer.preview.assert_awaited_once_with(self.h.guild, 99, 41,
                                                      before_id=None, thread_id=None, after_id=None, cursor=token)
        self.assertIn('0 подтверждённых книг в этом фрагменте', self.text())
        self.assertIn('завершено 1 из 3 тредов', self.text())
        self.assertIn('cursor:' + 'b' * 32, self.text())
        self.assertNotIn('/club import scan', self.text())
        self.assert_private_and_free()

    async def test_cursor_cannot_be_combined_with_independent_range(self):
        self.importer.preview = AsyncMock()
        for options in ({'after': '1'}, {'before': '99'}, {'thread': self.h.old_thread}):
            with self.subTest(options=options), self.assertRaisesRegex(ClubError, 'не совмещайте'):
                await Club.import_preview.callback(self.cog, self.ctx, cursor='a' * 32, **options)
        self.importer.preview.assert_not_awaited()

    async def test_archived_thread_id_can_be_inspected_before_metadata_confirmation(self):
        self.ctx.channel = self.h.source
        self.importer.preview = AsyncMock(return_value={
            'source_id': 41, 'books': [], 'messages': [], 'warnings': [], 'continuation': None,
            'preparation_confirmed': False,
            'coverage': {'complete': True, 'total_threads': 1, 'completed_threads': 1}})
        await Club.import_preview.callback(self.cog, self.ctx, thread='51')
        self.importer.preview.assert_awaited_once_with(self.h.guild, 99, 41,
                                                      before_id=None, thread_id=51, after_id=None)
        self.assertIn('Ознакомительный просмотр', self.text())
        self.assertIn('название книги, автор и включение треда ещё не подтверждены', self.text())
        self.assertNotIn('Подтверждённая очередь', self.text())
        self.assertNotIn('выбранных подтверждённых тредов завершён', self.text())
        self.assert_private_and_free()

    async def test_completed_range_is_not_presented_as_complete_archive(self):
        self.importer.preview = AsyncMock(return_value={
            'source_id': 41, 'books': [], 'messages': [], 'warnings': [], 'continuation': None,
            'coverage': {'complete': True, 'total_threads': 1, 'completed_threads': 1, 'range_limited': True}})
        await Club.import_preview.callback(self.cog, self.ctx)
        self.assertIn('не означает просмотр всего треда', self.text())
        self.assertIn('Исключённые и неподтверждённые треды в этот охват не входят', self.text())
        self.assert_private_and_free()

    def test_preparation_commands_register_within_discord_limits(self):
        self.assertLessEqual(len(Club.archive.app_command.commands), 25)
        for command in (Club.import_inventory, Club.import_prepare, Club.import_preview, Club.import_scan):
            self.assertLessEqual(len(command.app_command.parameters), 25)
        book_parameter = next(parameter for parameter in Club.import_prepare.app_command.parameters if parameter.name == 'book')
        self.assertTrue(book_parameter.autocomplete)


if __name__ == '__main__':
    unittest.main()
