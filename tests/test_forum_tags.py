"""Tag reconciliation preserves Discord identity and labels owned by humans."""
from copy import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from bookclub.forum_tags import ForumTags, TEMPLATES
from bookclub.store import ClubError


def tag(ident, name, *, moderated=False, emoji=None):
    result = discord.ForumTag(name=name, moderated=moderated, emoji=emoji)
    result.id = ident
    return result


class TagStore:
    def __init__(self):
        self.data, self.writes = {}, []

    def bindings(self, guild_id, forum_id):
        return [dict(purpose=purpose, tag_id=ident) for (guild, forum, purpose), ident in self.data.items()
                if (guild, forum) == (guild_id, forum_id)]

    def bind_tag(self, guild_id, forum_id, purpose, tag_id):
        self.data[guild_id, forum_id, purpose] = tag_id
        self.writes.append((guild_id, forum_id, purpose, tag_id))


class ForumTagTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=1, me=SimpleNamespace(id=100))
        self.store = TagStore()
        self.service = SimpleNamespace(store=self.store, channel=AsyncMock(),
                                       bot=SimpleNamespace(fetch_channel=AsyncMock()))
        self.tags = ForumTags(self.service)
        self.forum = self.make_forum()
        self.next_tag = 1000

    def make_forum(self, *, ident=10, tags=(), required=False):
        forum = Mock(spec=discord.ForumChannel)
        forum.id, forum.guild, forum.available_tags = ident, self.guild, list(tags)
        forum.flags = discord.ChannelFlags._from_value(16 if required else 0)
        forum.permissions_for.return_value = discord.Permissions.all()

        async def edit(**kwargs):
            values = []
            for original in kwargs['available_tags']:
                value = copy(original)
                if not value.id:
                    self.next_tag += 1
                    value.id = self.next_tag
                values.append(value)
            # discord.py edit returns a new instance, not an in-place change.
            return self.make_forum(ident=ident, tags=values, required=required)

        forum.edit = AsyncMock(side_effect=edit)
        return forum

    def make_thread(self, tags=(), *, archived=False):
        thread = Mock(spec=discord.Thread)
        thread.id, thread.guild, thread.parent, thread.parent_id = 20, self.guild, self.forum, self.forum.id
        thread.applied_tags, thread.archived = list(tags), archived

        async def edit(**kwargs):
            for key, value in kwargs.items():
                if key != 'reason':
                    setattr(thread, key, value)
            return thread

        thread.edit = AsyncMock(side_effect=edit)
        return thread

    async def test_creates_only_missing_tags_preserves_metadata_order_and_ids(self):
        custom = tag(50, 'Фантастика', moderated=True, emoji='📚')
        proposed = tag(51, 'Предложено', emoji='⌛')
        self.forum.available_tags = [custom, proposed]
        await self.tags.ensure(self.guild, self.forum, 'books')
        sent = self.forum.edit.call_args.kwargs['available_tags']
        self.assertEqual(sent[:2], [custom, proposed])
        self.assertEqual([value.to_dict() for value in sent[:2]], [custom.to_dict(), proposed.to_dict()])
        self.assertEqual(len(sent), 6)
        self.assertEqual(self.store.data[1, 10, 'proposed'], 51)
        self.assertEqual(len(self.store.data), 5)
        self.assertTrue(all(self.store.data[1, 10, purpose] for purpose in TEMPLATES['books']))

    async def test_restart_keeps_renamed_tag_without_recreating_original_name(self):
        await self.tags.ensure(self.guild, self.forum, 'books')
        current = self.tags.current(self.forum)
        reading = next(value for value in current.available_tags if value.id == self.store.data[1, 10, 'reading'])
        reading.name, reading.emoji = 'Сейчас читаем', discord.PartialEmoji(name='📖')
        restarted = ForumTags(self.service)
        self.store.writes.clear()
        await restarted.ensure(self.guild, current, 'books')
        current.edit.assert_not_awaited()
        self.assertEqual(restarted.creation_tags(1, current, 'book', 'reading'), [reading])
        self.assertEqual(self.store.writes, [])

    async def test_same_instance_repeat_uses_successful_edit_response(self):
        await self.tags.ensure(self.guild, self.forum, 'books')
        await self.tags.ensure(self.guild, self.forum, 'books')
        self.assertEqual(self.forum.edit.await_count, 1)
        self.tags.current(self.forum).edit.assert_not_awaited()
        self.assertTrue(self.tags.creation_tags(1, self.forum, 'catalog')[0].id)

    async def test_check_only_does_not_adopt_or_create(self):
        self.forum.available_tags = [tag(50, 'Черновик')]
        report = await self.tags.ensure(self.guild, self.forum, 'essays', check_only=True)
        self.assertIn('Эссе', report[0])
        self.assertFalse(self.store.writes)
        self.forum.edit.assert_not_awaited()

    async def test_duplicate_default_name_is_refused_before_any_mutation(self):
        self.forum.available_tags = [tag(50, 'Эссе'), tag(51, 'Эссе')]
        with self.assertRaisesRegex(ClubError, 'неоднозначно'):
            await self.tags.ensure(self.guild, self.forum, 'essays')
        self.assertFalse(self.store.writes)
        self.forum.edit.assert_not_awaited()

    async def test_bound_renamed_tag_wins_over_duplicate_default_names(self):
        self.store.bind_tag(1, 10, 'essay', 50)
        self.forum.available_tags = [tag(50, 'Мой текст'), tag(51, 'Эссе'), tag(52, 'Эссе')]
        await self.tags.ensure(self.guild, self.forum, 'essays')
        self.assertEqual(self.store.data[1, 10, 'essay'], 50)
        self.assertEqual(len(self.tags.current(self.forum).available_tags), 5)

    async def test_full_forum_refused_without_dropping_foreign_tags_or_partial_bindings(self):
        self.forum.available_tags = [tag(50, 'Черновик')] + [tag(100 + i, f'Чужой {i}') for i in range(18)]
        with self.assertRaisesRegex(ClubError, 'лимит Discord — 20'):
            await self.tags.ensure(self.guild, self.forum, 'essays')
        self.assertFalse(self.store.writes)
        self.forum.edit.assert_not_awaited()
        self.assertEqual(len(self.forum.available_tags), 19)

    async def test_deleted_tag_is_replaced_and_live_renamed_tag_is_preserved(self):
        self.store.bind_tag(1, 10, 'draft', 50)
        self.store.bind_tag(1, 10, 'essay', 51)
        renamed = tag(51, 'Готово')
        self.forum.available_tags = [renamed]
        await self.tags.ensure(self.guild, self.forum, 'essays')
        self.assertNotEqual(self.store.data[1, 10, 'draft'], 50)
        self.assertEqual(self.store.data[1, 10, 'essay'], 51)

    async def test_lost_acknowledgement_is_recovered_from_fresh_list_without_duplicates(self):
        normal_edit = self.forum.edit.side_effect
        saved = []

        async def lose_ack(**kwargs):
            saved.append(await normal_edit(**kwargs))
            raise OSError('response lost')

        self.forum.edit.side_effect = lose_ack
        with self.assertRaises(OSError):
            await self.tags.ensure(self.guild, self.forum, 'essays')
        self.assertFalse(self.store.writes)
        restarted = ForumTags(self.service)
        await restarted.ensure(self.guild, saved[0], 'essays')
        saved[0].edit.assert_not_awaited()
        self.assertEqual(len(self.store.data), 3)

    async def test_requires_manage_channels_only_when_missing(self):
        self.forum.permissions_for.return_value.manage_channels = False
        with self.assertRaisesRegex(ClubError, 'Manage Channels'):
            await self.tags.ensure(self.guild, self.forum, 'essays')
        self.forum.available_tags = [tag(50 + i, name) for i, name in enumerate(TEMPLATES['essays'].values())]
        await self.tags.ensure(self.guild, self.forum, 'essays')
        self.forum.edit.assert_not_awaited()

    async def test_required_tag_forum_gets_valid_creation_tags_after_setup(self):
        self.forum.flags.require_tag = True
        with self.assertRaisesRegex(ClubError, '/club setup'):
            self.tags.creation_tags(1, self.forum, 'draft')
        await self.tags.ensure(self.guild, self.forum, 'essays')
        self.assertEqual([value.id for value in self.tags.creation_tags(1, self.forum, 'draft')],
                         [self.store.data[1, 10, 'draft']])
        self.assertEqual([value.id for value in self.tags.creation_tags(1, self.forum, 'imported')],
                         [self.store.data[1, 10, 'essay'], self.store.data[1, 10, 'imported']])

    def test_legacy_optional_tags_forum_remains_usable_without_setup(self):
        self.assertEqual(self.tags.creation_tags(1, self.forum, 'draft'), [])

    def test_configured_deleted_tag_requires_repair_instead_of_silent_untagged_post(self):
        self.store.bind_tag(1, 10, 'draft', 50)
        with self.assertRaisesRegex(ClubError, '/club setup'):
            self.tags.creation_tags(1, self.forum, 'draft')

    async def test_sync_replaces_status_but_preserves_foreign_tags_and_import_provenance(self):
        draft, essay, imported, custom = tag(50, 'Черновик'), tag(51, 'Эссе'), tag(52, 'Архив'), tag(53, 'Личное')
        self.forum.available_tags = [draft, essay, imported, custom]
        for kind, value in [('draft', draft), ('essay', essay), ('imported', imported)]:
            self.store.bind_tag(1, 10, kind, value.id)
        thread = self.make_thread([draft, custom, imported])
        self.assertTrue(await self.tags.sync_thread_tags(self.guild, thread, ['essay']))
        self.assertEqual(thread.applied_tags, [custom, imported, essay])
        self.assertFalse(await self.tags.sync_thread_tags(self.guild, thread, ['essay']))
        self.assertEqual(thread.edit.await_count, 1)

    async def test_sync_refuses_overflow_without_discarding_any_foreign_tags(self):
        foreign = [tag(i, f'Личное {i}') for i in range(50, 55)]
        essay = tag(55, 'Эссе')
        self.forum.available_tags = foreign + [essay]
        self.store.bind_tag(1, 10, 'essay', essay.id)
        thread = self.make_thread(foreign)
        with self.assertRaisesRegex(ClubError, 'пользовательские теги сохранены'):
            await self.tags.sync_thread_tags(self.guild, thread, ['essay'])
        thread.edit.assert_not_awaited()
        self.assertEqual(thread.applied_tags, foreign)

    async def test_archived_thread_restored_after_tag_update(self):
        essay = tag(50, 'Эссе')
        self.forum.available_tags = [essay]
        self.store.bind_tag(1, 10, 'essay', essay.id)
        thread = self.make_thread(archived=True)
        await self.tags.sync_thread_tags(self.guild, thread, ['essay'])
        self.assertEqual(thread.edit.await_count, 2)
        self.assertFalse(thread.edit.call_args_list[0].kwargs['archived'])
        self.assertTrue(thread.edit.call_args_list[1].kwargs['archived'])
        self.assertTrue(thread.archived)

    async def test_cross_guild_forum_and_thread_rejected(self):
        another_guild = SimpleNamespace(id=2, me=self.guild.me)
        with self.assertRaises(ClubError):
            await self.tags.ensure(another_guild, self.forum, 'essays')
        with self.assertRaises(ClubError):
            self.tags.creation_tags(2, self.forum, 'essay')
        with self.assertRaises(ClubError):
            await self.tags.sync_thread_tags(another_guild, self.make_thread(), ['essay'])
        self.forum.edit.assert_not_awaited()

    async def test_distinct_stale_gateway_cache_refetches_newly_created_tags(self):
        await self.tags.ensure(self.guild, self.forum, 'essays')
        updated = self.tags.current(self.forum)
        stale_gateway_copy = self.make_forum()
        self.service.bot.fetch_channel.return_value = updated
        actual = await self.tags.fresh(self.guild, stale_gateway_copy)
        self.assertIs(actual, updated)
        self.service.bot.fetch_channel.assert_awaited_once_with(self.forum.id)
        self.assertTrue(self.tags.creation_tags(1, actual, 'draft')[0].id)

    async def test_deleted_bound_tag_still_requires_setup_after_http_refetch(self):
        self.store.bind_tag(1, 10, 'essay', 50)
        self.service.bot.fetch_channel.return_value = self.make_forum()
        thread = self.make_thread()
        with self.assertRaisesRegex(ClubError, '/club setup'):
            await self.tags.sync_thread_tags(self.guild, thread, ['essay'])
        self.service.bot.fetch_channel.assert_awaited_once_with(10)
        thread.edit.assert_not_awaited()

    async def test_refetch_rejects_wrong_type_and_guild(self):
        self.store.bind_tag(1, 10, 'essay', 50)
        wrong_type = Mock(spec=discord.TextChannel)
        wrong_type.guild = self.guild
        self.service.bot.fetch_channel.return_value = wrong_type
        with self.assertRaises(ClubError):
            await self.tags.fresh(self.guild, self.forum)
        wrong_guild = self.make_forum()
        wrong_guild.guild = SimpleNamespace(id=2)
        self.service.bot.fetch_channel.return_value = wrong_guild
        with self.assertRaises(ClubError):
            await self.tags.fresh(self.guild, self.forum)

    def real_thread_with_stale_parent(self, applied):
        """Use the real SDK property that drops tags absent in its parent cache."""
        self.guild.get_channel = lambda ident: self.forum if ident == self.forum.id else None
        self.forum.type = discord.ChannelType.forum
        self.forum.get_tag.side_effect = lambda ident: next(
            (value for value in self.forum.available_tags if value.id == ident), None)
        thread = object.__new__(discord.Thread)
        thread.id, thread.parent_id, thread.guild, thread.archived = 20, self.forum.id, self.guild, False
        thread._applied_tags = [value.id for value in applied]
        return thread

    async def test_real_sdk_filtered_property_does_not_remove_new_foreign_tag(self):
        proposed, reading, foreign = tag(50, 'Предложено'), tag(51, 'Читаем'), tag(52, 'Личное')
        self.forum.available_tags = [proposed, reading]
        self.store.bind_tag(1, 10, 'proposed', proposed.id)
        self.store.bind_tag(1, 10, 'reading', reading.id)
        thread = self.real_thread_with_stale_parent([proposed, foreign])
        self.assertEqual(thread.applied_tags, [proposed])  # Actual discord.py filtering.
        fresh = self.make_forum(tags=[proposed, reading, foreign])
        self.service.bot.fetch_channel.return_value = fresh
        with patch.object(discord.Thread, 'edit', new_callable=AsyncMock) as edit:
            self.assertTrue(await self.tags.sync_thread_tags(self.guild, thread, ['reading']))
            self.assertEqual(edit.await_args.kwargs['applied_tags'], [foreign, reading])
        self.service.bot.fetch_channel.assert_awaited_once_with(self.forum.id)

    async def test_unresolvable_raw_foreign_tag_blocks_mutation_after_refetch(self):
        proposed, reading, deleted = tag(50, 'Предложено'), tag(51, 'Читаем'), tag(52, 'Удалён')
        self.forum.available_tags = [proposed, reading]
        self.store.bind_tag(1, 10, 'proposed', proposed.id)
        self.store.bind_tag(1, 10, 'reading', reading.id)
        thread = self.real_thread_with_stale_parent([proposed, deleted])
        self.service.bot.fetch_channel.return_value = self.make_forum(tags=[proposed, reading])
        with patch.object(discord.Thread, 'edit', new_callable=AsyncMock) as edit:
            with self.assertRaisesRegex(ClubError, 'пользовательские теги сохранены'):
                await self.tags.sync_thread_tags(self.guild, thread, ['reading'])
            edit.assert_not_awaited()
        self.assertEqual(thread._applied_tags, [proposed.id, deleted.id])


if __name__ == '__main__':
    unittest.main()
