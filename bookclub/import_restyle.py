"""Reviewed, resumable restyling of completed imports without another model call."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord

from .import_publication import archive_chunks, archive_header, matches_clean_content
from .import_chunks import publication_chunks
from .store import ClubError


async def restyle(importer, guild, actor_id, run_id, *, confirm=False):
    service, store, ledger = importer.service, importer.store, importer.ledger
    actor = await importer.guard(guild, actor_id)
    async with service.locks[guild.id]:
        run = await importer.review(guild, actor_id, run_id)
        if run['state'] != 'done' or not run['plan']:
            raise ClubError('Оформление исправляется только у завершённого импорта с сохранённым планом.')
        if not store.settings(guild.id)['essay_webhooks']:
            raise ClubError('Для авторского оформления включите essay_webhooks в конфигурации клуба.')
        forum = await service.forum_access(guild, actor, 'essays')
        if forum.id != run['snapshot']['target_forum_id']:
            raise ClubError('Форум назначения изменился. Темы прежнего импорта не перемещены.')
        from .archive_import import validate_plan
        plan = validate_plan(run['snapshot'], {'essays': [
            {'book_ref': item['book_ref'], 'message_ids': item['message_ids']}
            for item in run['plan']['essays']]})
        snapshot_books = {book['ref']: book for book in run['snapshot']['books']}
        snapshots = {m['id']: m for m in run['snapshot']['messages']}
        prepared, byte_count, replacement_count, cleanup_count = [], 0, 0, 0
        for item in plan['essays']:
            ids = item['message_ids']
            key = f'essay-import:{guild.id}:{min(ids, key=int)}'
            bindings = [ledger.imported_source(guild.id, int(ident)) for ident in ids]
            if (any(not b or b['run_id'] != run_id or b['item_key'] != key or not b['thread_id'] for b in bindings)
                    or len({b['thread_id'] for b in bindings}) != 1):
                raise ClubError('Журнал исходных сообщений не подтверждает завершённый перенос этого эссе.')
            thread_id = bindings[0]['thread_id']
            essay = store.one('SELECT * FROM bc_essays WHERE guild_id=? AND source_id=? AND deleted=0', (guild.id, thread_id))
            pub = store.publication(key)
            if (not essay or essay['author_id'] != item['author_id'] or not pub or pub['guild_id'] != guild.id
                    or pub['channel_id'] != thread_id or pub['message_id'] != thread_id):
                raise ClubError('Автор или тема эссе не совпадает с журналом импорта.')
            thread = await service.bot.fetch_channel(thread_id)
            if not isinstance(thread, discord.Thread) or thread.guild.id != guild.id or thread.parent_id != forum.id:
                raise ClubError('Тема эссе находится вне настроенного форума.')
            if not service.can_read(thread, actor):
                raise ClubError('Нет доступа к теме эссе.')
            starter = await thread.fetch_message(thread.id)
            book = store.book(guild.id, essay['book_id'])
            header = archive_header(book, item['author_id'])
            marker = '\n-# bc:' + key
            known_headers = set()
            for known_book in (book, snapshot_books[item['book_ref']]):
                known_header = archive_header(known_book, item['author_id'])
                legacy_header = (known_header + '\nПеренесено из старого обсуждения по подтверждённому плану.\n'
                                 f'[Первое исходное сообщение]({snapshots[ids[0]]["url"]})')
                known_headers.update((known_header, known_header + marker, legacy_header, legacy_header + marker))
            if (not service.owns_starter(starter, pub['webhook_id'])
                    or starter.content not in known_headers):
                raise ClubError('Шапка эссе не принадлежит сохранённому импорту.')
            if starter.content != header:
                cleanup_count += 1
            if starter.content != header and pub['webhook_id'] is not None:
                try:
                    hook = await service.bot.fetch_webhook(pub['webhook_id'])
                except discord.NotFound as exc:
                    raise ClubError('Вебхук шапки удалён; изменить её невозможно. Прежние сообщения сохранены.') from exc
                if not service.valid_essay_webhook(hook, guild, forum.id) or not hook.token:
                    raise ClubError('Вебхук шапки не подтверждён; исправление остановлено.')
            parts, obsolete = [], []
            for ident in ids:
                old = snapshots[ident]
                desired = publication_chunks(store, guild.id, key, old)
                legacy = archive_chunks(old, legacy=True)
                old_copies = {}
                for index, text in enumerate(legacy):
                    legacy_key = f'{key}:message:{ident}:{index}'
                    saved = store.publication(legacy_key)
                    if not saved:
                        continue
                    if saved['guild_id'] != guild.id or saved['channel_id'] != thread.id or not saved['message_id']:
                        raise ClubError('Прежняя копия имеет неподтверждённую привязку; исправление остановлено.')
                    expected = text + f'\n[Оригинал сообщения]({old["url"]})'
                    expected_files = sorted((a['filename'], a['size']) for a in old['attachments'] if a['copy']) if index == 0 else []
                    try:
                        message = await thread.fetch_message(saved['message_id'])
                    except discord.NotFound:
                        message = None
                    if message is not None and (not service.owns_starter(message, saved['webhook_id'])
                                                or message.content != expected + '\n-# bc:' + legacy_key
                                                or sorted((a.filename, a.size) for a in message.attachments) != expected_files):
                        raise ClubError('Прежняя копия изменена или принадлежит другому автору. Она сохранена.')
                    old_copies[index] = message
                    obsolete.append((legacy_key, expected, ident, index))
                for index in range(len(legacy)):
                    # Do not resurrect a fragment someone removed, including
                    # the middle of a long essay. A durable v2 reservation can
                    # recover its already sent replacement without a resend.
                    if old_copies.get(index) is None and not store.publication(f'{key}:message:{ident}:{index}:v2'):
                        raise ClubError('Не найдена прежняя часть эссе. Исправление не восстанавливает удалённый пользователем текст.')
                for index, text in enumerate(desired):
                    copy_key = f'{key}:message:{ident}:{index}:v2'
                    expected_files = sorted((a['filename'], a['size']) for a in old['attachments'] if a['copy']) if index == 0 else []
                    copy = store.publication(copy_key)
                    if copy:
                        if copy['guild_id'] != guild.id or copy['channel_id'] != thread.id or copy['webhook_id'] is None:
                            raise ClubError('Новая копия имеет другую привязку или отправителя.')
                        if copy['message_id']:
                            message = await thread.fetch_message(copy['message_id'])
                            if (not service.owns_starter(message, copy['webhook_id'])
                                    or (not matches_clean_content(message.content, text)
                                        and message.content != text + '\n-# bc:' + copy_key)
                                    or sorted((a.filename, a.size) for a in message.attachments) != expected_files):
                                raise ClubError('Новая копия эссе изменена. Автоматическое исправление остановлено.')
                            if not matches_clean_content(message.content, text):
                                cleanup_count += 1
                    attachments = []
                    if index == 0 and not copy:
                        original = old_copies.get(0)
                        if original is None and expected_files:
                            raise ClubError('Копия с вложениями не найдена; файлы не будут пропущены.')
                        attachments = list(original.attachments) if original is not None else []
                        if sorted((a.filename, a.size) for a in attachments) != expected_files:
                            raise ClubError('Состав вложений прежней копии изменился; исправление остановлено.')
                        for attachment in attachments:
                            byte_count += attachment.size
                            if attachment.size > guild.filesize_limit or byte_count > importer.config.max_attachment_bytes:
                                raise ClubError('Вложения исправляемого импорта превышают текущий лимит; он не увеличен.')
                    if not copy or not copy['message_id']:
                        replacement_count += 1
                    parts.append((ident, index, copy_key, text, attachments))
            first = snapshots[ids[0]]
            member = SimpleNamespace(display_name=first['author_name'], display_avatar=SimpleNamespace(url=first['avatar_url']))
            prepared.append((thread, starter, pub, book, item['author_id'], member, parts, obsolete))
        report = [f'**Исправление оформления импорта** `{run_id}`',
                  f'Тем: {len(prepared)}. Частей для отправки или восстановления: {replacement_count}.',
                  f'Существующих сообщений для очистки: {cleanup_count}.',
                  'Темы и ответы участников сохраняются. Служебные ссылки и метки импорта убираются; '
                  'текст и файлы публикуются с ником и аватаркой автора. Существующие авторские копии редактируются на месте. '
                  'Если нужны новые копии, они появятся в конце существующих тем.',
                  'Codex не вызывается; квота и состояние завершённого импорта сохраняются.']
        report.extend(f'<#{thread.id}> · частей эссе: {len(parts)}' for thread, _, _, _, _, _, parts, _ in prepared)
        if not confirm:
            return report + [f'Подтверждение: /club import restyle run:{run_id} confirm:true']
        # Freeze every verified layout before Discord writes. Existing v2 parts
        # keep their old boundaries and IDs; new applies already own a plan.
        for item in plan['essays']:
            key = f'essay-import:{guild.id}:{min(item["message_ids"], key=int)}'
            for ident in item['message_ids']:
                publication_chunks(store, guild.id, key, snapshots[ident], persist=True)
        for thread, starter, pub, book, author_id, member, parts, obsolete in prepared:
            archived = thread.archived or store.one(
                'SELECT 1 FROM bc_import_restyle_threads WHERE guild_id=? AND run_id=? AND thread_id=?',
                (guild.id, run_id, thread.id)) is not None
            if archived:
                with store.tx() as db:
                    db.execute('INSERT OR IGNORE INTO bc_import_restyle_threads(guild_id,run_id,thread_id) VALUES(?,?,?)',
                               (guild.id, run_id, thread.id))
            try:
                if thread.archived:
                    thread = await thread.edit(archived=False)
                replacements = {}
                for ident, index, copy_key, text, attachments in parts:
                    files = []
                    try:
                        for attachment in attachments:
                            files.append(await asyncio.wait_for(attachment.to_file(), timeout=30))
                        expected_files = [(a['filename'], a['size']) for a in snapshots[ident]['attachments'] if a['copy']] if index == 0 else []
                        copy = await importer.publisher.upsert(guild, thread, copy_key, text, member, files=files,
                                                              expected_attachments=expected_files)
                        replacements[(ident, index)] = copy
                    finally:
                        for file in files:
                            file.close()
                header = archive_header(book, author_id)
                if starter.content != header:
                    await service.edit_essay_starter(thread, starter, header, pub)
                    current = await thread.fetch_message(starter.id)
                    if not service.owns_starter(current, pub['webhook_id']) or current.content != header:
                        raise ClubError('Обновление шапки не подтверждено. Прежние сообщения сохранены.')
                    importer.publisher.audit(guild.id, run_id, actor_id, 'header-updated', thread.id, starter.id, starter.id)
                current = await thread.fetch_message(starter.id)
                await service.finish_import_header(thread, current, header, pub)
                # All replacements are durably acknowledged before retiring
                # any old body; other participants' messages are never selected.
                for legacy_key, expected, ident, index in obsolete:
                    replacement = replacements.get((ident, index)) or next(
                        copy for (source_id, _), copy in replacements.items() if source_id == ident)
                    expected_files = [(a['filename'], a['size']) for a in snapshots[ident]['attachments'] if a['copy']] if index == 0 else []
                    await importer.publisher.retire_legacy(guild, run_id, actor_id, thread, legacy_key, replacement, expected,
                                                          expected_attachments=expected_files)
                await service.register_thread(thread, prompt=False)
            finally:
                if archived:
                    await thread.edit(archived=True)
                    with store.tx() as db:
                        db.execute('DELETE FROM bc_import_restyle_threads WHERE guild_id=? AND run_id=? AND thread_id=?',
                                   (guild.id, run_id, thread.id))
        # Remove only the exact automatically generated archive URL; preserve
        # any materials a person supplied or edited after the import.
        for candidate in run['snapshot']['books']:
            if candidate.get('source_url') and not candidate.get('book_id'):
                book = store.one('SELECT * FROM bc_books WHERE guild_id=? AND request_key=?',
                                 (guild.id, 'archive:' + candidate['ref']))
                if book and book['materials'] == candidate['source_url']:
                    store.update_book(guild.id, book['id'], materials='')
        if store.settings(guild.id)['published']:
            await service.refresh(guild)
        return report + ['Оформление исправлено. Темы, оригиналы и квота сохранены; повторная команда не создаст копии заново.']
