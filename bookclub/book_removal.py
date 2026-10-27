"""Confirmed book removal plans and a durable, scoped Discord work journal.

This module performs no Discord I/O. Existing essay identities and import
bindings are retained even when their visible Discord resources are removed.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .store import ClubError


MODES = {'keep', 'topic', 'all', 'transfer', 'transfer_topic'}
TRANSFER_MODES = {'transfer', 'transfer_topic'}
TOPIC_MODES = {'topic', 'all', 'transfer_topic'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS bc_book_removal_operations(
      id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,
      book_id TEXT NOT NULL REFERENCES bc_books(id),
      target_book_id TEXT REFERENCES bc_books(id), mode TEXT NOT NULL,
      actor_id INTEGER NOT NULL, confirmed_actor_id INTEGER NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('pending','failed','done')),
      plan TEXT NOT NULL, before_state TEXT NOT NULL, after_state TEXT NOT NULL,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS bc_book_removal_resources(
      id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES bc_book_removal_operations(id),
      guild_id INTEGER NOT NULL, kind TEXT NOT NULL,
      channel_id INTEGER, source_id INTEGER, message_id INTEGER,
      publication_key TEXT, target_book_id TEXT, data TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('pending','deleting','failed','done')),
      attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at INTEGER NOT NULL)''')
    db.execute('''CREATE INDEX IF NOT EXISTS bc_book_removal_book
      ON bc_book_removal_operations(guild_id,book_id,state)''')
    db.execute('''CREATE INDEX IF NOT EXISTS bc_book_removal_resource_source
      ON bc_book_removal_resources(guild_id,source_id,kind)''')
    db.execute('''CREATE TABLE IF NOT EXISTS bc_book_removal_retries(
      id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES bc_book_removal_operations(id),
      guild_id INTEGER NOT NULL, actor_id INTEGER NOT NULL, previous_actor_id INTEGER NOT NULL,
      previous_resources TEXT NOT NULL, created_at INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS bc_book_removal_tombstones(
      guild_id INTEGER NOT NULL, source_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
      operation_id TEXT NOT NULL REFERENCES bc_book_removal_operations(id),
      PRIMARY KEY(guild_id,source_id))''')
    db.execute('INSERT INTO bc_migrations(version) VALUES(13)')


def _actor(actor_id):
    if type(actor_id) is not int or actor_id <= 0:
        raise ClubError('Для подтверждения нужен участник-организатор.')


def _not_busy(db, guild_id, book_id):
    if db.execute('''SELECT 1 FROM bc_book_removal_operations WHERE guild_id=?
      AND state<>'done' AND (book_id=? OR target_book_id=?)''',
      (guild_id, book_id, book_id)).fetchone():
        raise ClubError('Для этой книги ещё не завершена операция удаления или переноса. '
                        'Откройте её статус в «Удалённых книгах».')


def _essays(db, guild_id, book_id):
    return [dict(row) for row in db.execute('''SELECT * FROM bc_essays
      WHERE guild_id=? AND book_id=? ORDER BY source_id''', (guild_id, book_id))]


def _fingerprint(rows):
    return hashlib.sha256(_json(rows).encode('utf-8')).hexdigest()


def _resource(plan_id, kind, *, essay=None, publication=None, forum_id=None, target_id=None):
    source_id = essay['source_id'] if essay else None
    suffix = str(source_id) if essay else 'book'
    return dict(id=f'{plan_id}:{kind}:{suffix}', kind=kind,
                channel_id=essay['channel_id'] if essay else publication['channel_id'],
                source_id=source_id,
                message_id=essay['source_id'] if essay else publication['message_id'],
                publication_key=publication['key'] if publication else None,
                target_book_id=target_id, essay=essay, publication=publication,
                forum_id=forum_id)


def _build(store, db, guild_id, source_id, mode, target_id, plan_id):
    if mode not in MODES:
        raise ClubError('Выберите способ удаления заново.')
    source = store._active_book(db, guild_id, source_id)
    _not_busy(db, guild_id, source_id)
    target = None
    if mode in TRANSFER_MODES:
        if not target_id or target_id == source_id:
            raise ClubError('Для переноса выберите другую книгу.')
        target = store._active_book(db, guild_id, target_id)
        _not_busy(db, guild_id, target_id)
    elif target_id is not None:
        raise ClubError('Книга назначения допустима только при переносе эссе.')
    settings_row = db.execute('SELECT data FROM bc_settings WHERE guild_id=?', (guild_id,)).fetchone()
    settings = json.loads(settings_row['data']) if settings_row else {}
    essays = _essays(db, guild_id, source_id)
    target_essays = _essays(db, guild_id, target_id) if target else []
    publication_row = db.execute('SELECT * FROM bc_publications WHERE guild_id=? AND key=?',
                                 (guild_id, f'book:{source_id}')).fetchone()
    publication = dict(publication_row) if publication_row else None
    # A confirmation must not silently omit an uncertain in-flight publication.
    if db.execute('''SELECT 1 FROM bc_publication_intents i
      JOIN bc_publications p ON p.key=i.key
      WHERE i.guild_id=? AND p.state='reserved' AND (i.key=? OR i.key LIKE ? OR i.channel_id IN
        (SELECT channel_id FROM bc_essays WHERE guild_id=? AND book_id=?))''',
      (guild_id, f'book:{source_id}', f'essay-space:{source_id}:%', guild_id, source_id)).fetchone():
        raise ClubError('Публикация книги или эссе ещё не завершена. Дождитесь обновления и откройте удаление заново.')
    if publication and publication['state'] != 'ready':
        raise ClubError('Публикация книги ещё не подтверждена. Восстановите её перед удалением.')
    live = [essay for essay in essays if not essay['deleted']]
    if mode in {'topic', 'transfer_topic'} and publication and any(
            essay['channel_id'] == publication['channel_id'] for essay in live):
        raise ClubError('В теме книги есть эссе; выберите сохранение темы, чтобы не удалить их текст.')
    resources = []
    def essay_publication(essay):
        rows = db.execute('''SELECT * FROM bc_publications WHERE guild_id=?
          AND channel_id=? AND message_id=?''', (guild_id, essay['channel_id'], essay['source_id'])).fetchall()
        if len(rows) > 1:
            raise ClubError('Найдены неоднозначные привязки эссе. Сначала проверьте их с организатором.')
        return dict(rows[0]) if rows else None
    if mode == 'all':
        for essay in live:
            resources.append(_resource(plan_id, 'delete_essay', essay=essay,
                                       publication=essay_publication(essay), forum_id=settings.get('essays')))
    elif mode in TRANSFER_MODES:
        for essay in live:
            resources.append(_resource(plan_id, 'refresh_essay', essay=essay, publication=essay_publication(essay),
                                       forum_id=settings.get('essays'), target_id=target_id))
    if mode in TOPIC_MODES and publication and publication['message_id'] and publication['channel_id']:
        resources.append(_resource(plan_id, 'delete_book_topic', publication=publication,
                                   forum_id=settings.get('books')))
    return dict(version=1, id=plan_id, guild_id=guild_id, mode=mode, source=source, target=target,
                essays=essays, essay_fingerprint=_fingerprint(essays),
                target_essay_fingerprint=_fingerprint(target_essays),
                meetings_count=db.execute('SELECT COUNT(*) FROM bc_meetings WHERE guild_id=? AND book_id=?',
                                          (guild_id, source_id)).fetchone()[0],
                overlapping_authors=sorted({e['author_id'] for e in live}
                    & {e['author_id'] for e in target_essays if not e['deleted']}),
                resources=resources,
                delete_threads=[r for r in resources if r['kind'].startswith('delete_')])


def build_removal_plan(store, guild_id, source_id, mode, target_id=None):
    with store.tx() as db:
        return _build(store, db, guild_id, source_id, mode, target_id, uuid.uuid4().hex)


def _decode_operation(row):
    operation = dict(row)
    for field in ('plan', 'before_state', 'after_state'):
        operation[field] = json.loads(operation[field])
    operation['operation_id'] = operation['id']
    operation['source_book_id'] = operation['book_id']
    return operation


def _operation(db, guild_id, operation_id):
    row = db.execute('SELECT * FROM bc_book_removal_operations WHERE guild_id=? AND id=?',
                     (guild_id, operation_id)).fetchone()
    if not row:
        raise ClubError('Операция удаления не найдена на этом сервере.')
    return _decode_operation(row)


def commit_removal_plan(store, guild_id, actor_id, plan):
    _actor(actor_id)
    if not isinstance(plan, dict) or plan.get('guild_id') != guild_id:
        raise ClubError('Откройте подтверждение удаления заново.')
    try:
        plan_id = plan['id']
        if uuid.UUID(plan_id).hex != plan_id:
            raise ValueError()
        source_id = plan['source']['id']
        target_id = plan['target']['id'] if plan['target'] else None
        mode = plan['mode']
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ClubError('Откройте подтверждение удаления заново.') from exc
    with store.tx() as db:
        if db.execute('SELECT 1 FROM bc_book_removal_operations WHERE id=?', (plan_id,)).fetchone():
            raise ClubError('Это подтверждение уже выполнено. Откройте статус операции.')
        fresh = _build(store, db, guild_id, source_id, mode, target_id, plan_id)
        if _json(fresh) != _json(plan):
            raise ClubError('Книга, эссе или их публикации изменились. Откройте подтверждение удаления заново.')
        before = dict(source=fresh['source'], target=fresh['target'], essays=fresh['essays'])
        if mode in TRANSFER_MODES:
            db.execute('UPDATE bc_essays SET book_id=? WHERE guild_id=? AND book_id=?',
                       (target_id, guild_id, source_id))
            db.execute('UPDATE bc_books SET revision=revision+1 WHERE guild_id=? AND id=?',
                       (guild_id, target_id))
        elif mode == 'all':
            db.execute('UPDATE bc_essays SET deleted=1 WHERE guild_id=? AND book_id=?', (guild_id, source_id))
        db.execute('''UPDATE bc_books SET deleted=1,status_automation=0,
          status_automation_pending=0,revision=revision+1 WHERE guild_id=? AND id=?''', (guild_id, source_id))
        db.execute('''UPDATE bc_jobs SET state='cancelled' WHERE guild_id=? AND state='pending'
          AND (entity_id=? OR entity_id IN (SELECT id FROM bc_meetings WHERE book_id=?))''',
          (guild_id, source_id, source_id))
        db.execute('''INSERT INTO bc_book_trash_audit
          (book_id,guild_id,actor_id,action,previous_state,created_at) VALUES(?,?,?,'remove',?,?)''',
          (source_id, guild_id, actor_id, _json(fresh['source']), store.clock()))
        after = dict(source=store._get(db, 'bc_books', guild_id, source_id),
                     target=store._get(db, 'bc_books', guild_id, target_id) if target_id else None,
                     essays=[dict(db.execute('SELECT * FROM bc_essays WHERE guild_id=? AND source_id=?',
                                              (guild_id, essay['source_id'])).fetchone())
                             for essay in fresh['essays']])
        state = 'pending' if fresh['resources'] else 'done'
        stamp = store.clock()
        db.execute('''INSERT INTO bc_book_removal_operations
          (id,guild_id,book_id,target_book_id,mode,actor_id,confirmed_actor_id,state,plan,
           before_state,after_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (plan_id, guild_id, source_id, target_id, mode, actor_id, actor_id, state,
           _json(fresh), _json(before), _json(after), stamp, stamp))
        for resource in fresh['resources']:
            db.execute('''INSERT INTO bc_book_removal_resources
              (id,operation_id,guild_id,kind,channel_id,source_id,message_id,publication_key,
               target_book_id,data,state,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?)''',
              (resource['id'], plan_id, guild_id, resource['kind'], resource['channel_id'],
               resource['source_id'], resource['message_id'], resource['publication_key'],
               resource['target_book_id'], _json(resource), stamp))
        if mode == 'all':
            # Also retain the identity of essays already marked missing before
            # confirmation: delayed discovery must not revive those later.
            db.executemany('''INSERT OR IGNORE INTO bc_book_removal_tombstones
              (guild_id,source_id,channel_id,operation_id) VALUES(?,?,?,?)''',
              [(guild_id, essay['source_id'], essay['channel_id'], plan_id) for essay in fresh['essays']])
        return _operation(db, guild_id, plan_id)


def get_removal_operation(store, guild_id, operation_id):
    with store.tx() as db:
        return _operation(db, guild_id, operation_id)


def removal_operations(store, guild_id, book_id=None, pending_only=True):
    query = 'SELECT * FROM bc_book_removal_operations WHERE guild_id=?'
    args = [guild_id]
    if book_id is not None:
        query += ' AND (book_id=? OR target_book_id=?)'
        args.extend((book_id, book_id))
    if pending_only:
        query += " AND state='pending'"
    return [_decode_operation(row) for row in store.rows(query + ' ORDER BY created_at,id', args)]


def latest_book_removal_operation(store, guild_id, book_id):
    rows = store.rows('''SELECT * FROM bc_book_removal_operations WHERE guild_id=? AND book_id=?
      ORDER BY created_at DESC,rowid DESC LIMIT 1''', (guild_id, book_id))
    return _decode_operation(rows[0]) if rows else None


def removal_resources(store, guild_id, operation_id, pending_only=True):
    get_removal_operation(store, guild_id, operation_id)
    query = 'SELECT * FROM bc_book_removal_resources WHERE guild_id=? AND operation_id=?'
    if pending_only:
        query += " AND state IN ('pending','deleting')"
    result = []
    for row in store.rows(query + ' ORDER BY rowid', (guild_id, operation_id)):
        data = json.loads(row.pop('data'))
        result.append({**data, **row})
    return result


def remember_removal_archive_state(store, guild_id, operation_id, resource_id, archived):
    """Persist the first observed archive state before any Discord thread edit.

    Resource data is the execution journal, distinct from the confirmed plan.
    Failed attempts and explicit retries retain it, including across restarts.
    """
    if type(archived) is not bool:
        raise ValueError('Archive state must be a boolean')
    with store.tx() as db:
        operation = _operation(db, guild_id, operation_id)
        resource = db.execute('''SELECT * FROM bc_book_removal_resources
          WHERE guild_id=? AND operation_id=? AND id=?''',
          (guild_id, operation_id, resource_id)).fetchone()
        if (not resource or resource['kind'] != 'refresh_essay'
                or resource['state'] != 'pending' or operation['state'] != 'pending'):
            raise ClubError('Перенос эссе не ожидает выполнения. Откройте статус операции.')
        data = json.loads(resource['data'])
        if 'original_archived' not in data:
            data['original_archived'] = archived
            stamp = store.clock()
            db.execute('UPDATE bc_book_removal_resources SET data=?,updated_at=? WHERE id=?',
                       (_json(data), stamp, resource_id))
            db.execute('UPDATE bc_book_removal_operations SET updated_at=? WHERE id=?',
                       (stamp, operation_id))
        if type(data['original_archived']) is not bool:
            raise ClubError('Исходное состояние архива повреждено; проверьте журнал операции.')
        return data['original_archived']


def set_removal_resource_state(store, guild_id, operation_id, resource_id, state, reason=None):
    state = 'failed' if state == 'paused' else state
    if state not in {'pending', 'deleting', 'failed', 'done'}:
        raise ValueError('Unknown removal resource state')
    with store.tx() as db:
        operation = _operation(db, guild_id, operation_id)
        resource = db.execute('''SELECT * FROM bc_book_removal_resources
          WHERE guild_id=? AND operation_id=? AND id=?''', (guild_id, operation_id, resource_id)).fetchone()
        if not resource:
            raise ClubError('Часть операции удаления не найдена на этом сервере.')
        if resource['state'] == 'done':
            return operation
        if resource['state'] == 'failed' and state != 'failed':
            raise ClubError('Сначала явно повторите операцию после проверки причины ошибки.')
        stamp = store.clock()
        db.execute('''UPDATE bc_book_removal_resources SET state=?,last_error=?,updated_at=?,
          attempts=attempts+? WHERE id=?''',
          (state, str(reason)[:240] if reason else None, stamp,
           int(state == 'deleting' and resource['state'] != 'deleting'), resource_id))
        outstanding = db.execute('''SELECT state FROM bc_book_removal_resources
          WHERE operation_id=? AND state<>'done' ''', (operation_id,)).fetchall()
        operation_state = 'failed' if any(row['state'] == 'failed' for row in outstanding) else (
            'pending' if outstanding else 'done')
        db.execute('UPDATE bc_book_removal_operations SET state=?,updated_at=? WHERE id=?',
                   (operation_state, stamp, operation_id))
        return _operation(db, guild_id, operation_id)


def complete_removal_resource(store, guild_id, operation_id, resource_id):
    return set_removal_resource_state(store, guild_id, operation_id, resource_id, 'done')


def fail_removal_resource(store, guild_id, operation_id, resource_id, reason):
    return set_removal_resource_state(store, guild_id, operation_id, resource_id, 'failed', reason)


def retry_removal_operation(store, guild_id, operation_id, actor_id):
    _actor(actor_id)
    with store.tx() as db:
        operation = _operation(db, guild_id, operation_id)
        if operation['state'] == 'done':
            return operation
        if operation['state'] != 'failed':
            raise ClubError('Операция ещё выполняется. Обновите её статус позже.')
        failed = [dict(row) for row in db.execute('''SELECT * FROM bc_book_removal_resources
          WHERE operation_id=? AND state='failed' ORDER BY rowid''', (operation_id,))]
        stamp = store.clock()
        db.execute('''INSERT INTO bc_book_removal_retries
          (operation_id,guild_id,actor_id,previous_actor_id,previous_resources,created_at)
          VALUES(?,?,?,?,?,?)''', (operation_id, guild_id, actor_id, operation['actor_id'], _json(failed), stamp))
        db.execute('''UPDATE bc_book_removal_resources SET state='pending',last_error=NULL,
          updated_at=? WHERE operation_id=? AND state='failed' ''', (stamp, operation_id))
        db.execute("UPDATE bc_book_removal_operations SET state='pending',actor_id=?,updated_at=? WHERE id=?",
                   (actor_id, stamp, operation_id))
        return _operation(db, guild_id, operation_id)


def assert_restore_allowed(db, guild_id, book_id):
    _not_busy(db, guild_id, book_id)


def assert_essay_registration_allowed(db, guild_id, source_id, channel_id):
    # A removed forum thread also owns all messages subsequently observed in it.
    if db.execute('''SELECT 1 FROM bc_book_removal_tombstones WHERE guild_id=?
      AND (source_id=? OR (source_id=channel_id AND channel_id=?))''',
      (guild_id, source_id, channel_id)).fetchone():
        raise ClubError('Это эссе удалено по подтверждённому плану; его нельзя зарегистрировать повторно.')
