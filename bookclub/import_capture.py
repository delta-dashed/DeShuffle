"""Bounded, resumable reads of explicitly confirmed book discussions.

The queue and exclusive message boundary are persisted independently of model
budgets. A cursor advances only past consumed messages, including ignored ones;
the first message which does not fit remains in the next page.
"""
from __future__ import annotations

import json
import uuid

import discord

from .store import ClubError


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


async def capture_prepared(importer, guild, actor, source, before_id=None,
                           thread_id=None, after_id=None, cursor=None, *, inspection=False):
    from .archive_import import fingerprint

    config, preparation = importer.config, importer.preparation.store
    for value in (before_id, thread_id, after_id):
        if value is not None and (type(value) is not int or not 0 < value < 2**63):
            raise ClubError('Диапазон: укажите числовые ID сообщений и треда Discord.')
    if cursor and any(value is not None for value in (before_id, thread_id, after_id)):
        raise ClubError('cursor уже содержит диапазон; не совмещайте его с thread/after/before.')
    if after_id is not None and thread_id is None:
        raise ClubError('Для after выберите конкретный thread.')
    if before_id is not None and thread_id is None:
        raise ClubError('Для before выберите конкретный thread. Весь выбранный архив продолжается через cursor.')
    if after_id is not None and before_id is not None and after_id >= before_id:
        raise ClubError('Диапазон пуст: after должен быть меньше before.')

    revision = preparation.revision(guild.id, source.id)
    if cursor:
        state = preparation.load_cursor(guild.id, source.id, cursor)
        if state['revision'] != revision:
            raise ClubError('Выбор книг изменён после предпросмотра. Начните новый preview без cursor.')
        if state['inspection'] and not inspection:
            raise ClubError('Это ознакомительный предпросмотр без подтверждённой книги. Сначала /club import prepare.')
    else:
        all_selections = preparation.selections(guild.id, source.id)
        selections = [s for s in all_selections
                      if s['decision'] == 'included' and (thread_id is None or int(s['thread_id']) == thread_id)]
        raw_inspection = inspection and thread_id is not None and not selections
        if raw_inspection and any(int(s['thread_id']) == thread_id and s['decision'] == 'excluded' for s in all_selections):
            raise ClubError('Тред исключён из подготовки. Для повторной проверки явно измените решение на pending.')
        if not selections and not raw_inspection:
            raise ClubError('Нет подтверждённых книжных тредов. Сначала /club import inventory, затем '
                            '/club import prepare: включите тред и подтвердите название/автора или существующую книгу.')
        queue = [{'thread_id': str(thread_id), 'book': None}] if raw_inspection else []
        for selection in sorted(selections, key=lambda s: int(s['thread_id']), reverse=True):
            book_id = selection.get('book_id')
            if book_id:
                book = importer.store.book(guild.id, book_id)
                title, author = book['title'], book['author']
            else:
                title, author = selection['title'], selection['author']
            ident = str(selection['thread_id'])
            queue.append({'thread_id': ident, 'book': {
                'ref': 'book:' + book_id if book_id else 'source:' + ident,
                'book_id': book_id, 'title': title, 'author': author,
                'source_url': f'https://discord.com/channels/{guild.id}/{ident}'}})
        state = {'revision': revision, 'queue': queue, 'index': 0, 'after': after_id,
                 'inspection': raw_inspection,
                 # Freeze the upper boundary so later messages don't change the queue's range.
                 'before': before_id or discord.utils.time_snowflake(discord.utils.utcnow(), high=True),
                 'range_limited': before_id is not None or after_id is not None}

    queue, index, after = state['queue'], state['index'], state['after']
    token = uuid.uuid4().hex
    snapshot = {'source_id': source.id, 'books': [], 'messages': [], 'warnings': [],
                'target_forum_id': importer.store.settings(guild.id)['essays'],
                'preparation_revision': revision, 'preparation_confirmed': not state['inspection'],
                'continuation': None, 'coverage': {}}
    attachment_bytes, raw_count, visited = 0, 0, 0
    completed = []
    consumed = set()
    attachment_warning = 'Вложения сверх лимита не копируются: сохранятся имя файла и отметка о лимите.'

    def decorate(position, last, complete=False):
        snapshot['continuation'] = None if complete else token
        snapshot['coverage'] = {'scope': 'unconfirmed_thread' if state['inspection'] else 'confirmed_threads',
                                'total_threads': len(queue),
                                'completed_threads': position,
                                'current_thread': None if complete else queue[position]['thread_id'],
                                'after': str(last) if last is not None and not complete else None,
                                'complete': complete, 'range_limited': state['range_limited']}
        snapshot['warnings'] = []
        if state['inspection']:
            snapshot['warnings'].append('Ознакомительный фрагмент: книга ещё не подтверждена. '
                                        'Проверьте текст и используйте /club import prepare перед анализом.')
        if any(not a['copy'] for m in snapshot['messages'] for a in m['attachments']):
            snapshot['warnings'].append(attachment_warning)
        if not complete:
            snapshot['warnings'].append('Достигнут лимит фрагмента; остальные сообщения и треды ещё не просмотрены. '
                                        f'Продолжение: /club import preview source:{source.id} cursor:{token}')

    bot_member = await guild.fetch_member(importer.service.bot.user.id)
    limited = False
    while index < len(queue):
        if visited >= config.max_threads or raw_count >= config.max_messages:
            limited = True
            break
        item = queue[index]
        thread = await importer.service.bot.fetch_channel(int(item['thread_id']))
        if (not isinstance(thread, discord.Thread) or thread.guild.id != guild.id
                or thread.parent_id != source.id or thread.is_private()):
            raise ClubError('Подтверждённый тред больше не является публичным тредом исходного канала. '
                            'Проверьте inventory и выбор книг.')
        for member in (actor, bot_member):
            perms = thread.permissions_for(member)
            if not perms.view_channel or not perms.read_message_history:
                raise ClubError('У вас или бота нет доступа к истории выбранного треда.')
        visited += 1
        async for message in thread.history(limit=config.max_messages - raw_count + 1, oldest_first=True,
                                            after=discord.Object(id=after) if after else None,
                                            before=discord.Object(id=state['before'])):
            if raw_count >= config.max_messages:
                limited = True
                break
            # Every consumed row advances the cursor, even when no essay is copied.
            if (message.author.bot or message.webhook_id is not None
                    or not (message.content.strip() or message.attachments)
                    or (message.id == thread.id and isinstance(source, discord.TextChannel))
                    or importer.ledger.imported_source(guild.id, message.id)):
                raw_count += 1
                consumed.add(thread.id)
                after = message.id
                continue
            attachments, next_attachment_bytes = [], attachment_bytes
            for attachment in message.attachments:
                copy = (next_attachment_bytes + attachment.size <= config.max_attachment_bytes
                        and attachment.size <= guild.filesize_limit)
                if copy:
                    next_attachment_bytes += attachment.size
                attachments.append({'id': str(attachment.id), 'filename': attachment.filename,
                                    'size': attachment.size, 'copy': copy})
            book = item['book']
            row = {'id': str(message.id), 'channel_id': thread.id, 'author_id': message.author.id,
                   'author_name': message.author.display_name, 'avatar_url': str(message.author.display_avatar.url),
                   'content': message.content, 'attachments': attachments, 'fingerprint': fingerprint(message),
                   'url': message.jump_url, 'context_ref': book['ref'] if book else None}
            new_book = book is not None and not any(b['ref'] == book['ref'] for b in snapshot['books'])
            if new_book:
                snapshot['books'].append(book)
            snapshot['messages'].append(row)
            decorate(index, message.id)
            # Moving to the next thread can widen cursor fields. Reserve their
            # exact maximum representation too, without trimming essay text.
            coverage = snapshot['coverage']
            widest = {**coverage, 'completed_threads': len(queue),
                      'current_thread': str(2**63 - 1), 'after': str(2**63 - 1)}
            required_bytes = encoded_size(snapshot) + max(0, encoded_size(widest) - encoded_size(coverage))
            if required_bytes > config.max_input_bytes:
                snapshot['messages'].pop()
                if new_book:
                    snapshot['books'].pop()
                if not snapshot['messages']:
                    raise ClubError(f'Сообщение {message.id} вместе с подтверждёнными метаданными не помещается '
                                    f'в лимит {config.max_input_bytes} байт. Текст не обрезан, позиция не пропущена. '
                                    'Нужен отдельный разбор этого сообщения человеком; лимиты не изменены.')
                limited = True
                break
            attachment_bytes = next_attachment_bytes
            raw_count += 1
            consumed.add(thread.id)
            after = message.id
        if limited:
            break
        completed.append(thread.id)
        index += 1
        after = None

    complete = index == len(queue)
    decorate(index, after, complete)
    if encoded_size(snapshot) > config.max_input_bytes:
        raise ClubError('Метаданные предпросмотра не помещаются в лимит входа. Выберите один подтверждённый thread.')
    coverage_updates = {ident: 'partial' if state['range_limited'] else 'complete' for ident in completed}
    if not complete:
        current = int(queue[index]['thread_id'])
        coverage_updates[current] = 'partial' if current in consumed else 'limited'
        for entry in queue[index + 1:]:
            coverage_updates[int(entry['thread_id'])] = 'limited'
    # Commit progress only if the same human selection is still current.
    preparation.finalize(guild.id, source.id, revision, coverage_updates,
                         token=None if complete else token,
                         payload=None if complete else {**state, 'index': index, 'after': after})
    return snapshot
