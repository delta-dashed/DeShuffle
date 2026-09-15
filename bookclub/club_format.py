"""Editable club-wide rules, with immutable revisions and derived essay dates."""
import json
from .store import ClubError, checked_text


DEFAULT_READING_MEETINGS = 3
DEFAULT_BODY = (
    'Читаем книги в порядке каталога. Обычно проводим три встречи по книге и одну встречу для обсуждения эссе. '
    'Для конкретной книги организатор может изменить план.\n\n'
    'К встрече читаем до указанной главы включительно. Спойлеры дальше границы чтения скрываем через ||текст||. '
    'Камеры включены, микрофоны исправны.\n\n'
    'Эссе публикуем через «Добавить своё эссе» в карточке книги. '
    'Перед обсуждением читаем работы друг друга.'
)


def reading_count(book):
    value = book.get('reading_meetings')
    return DEFAULT_READING_MEETINGS if value is None else value


def get_format(store, guild_id):
    store.settings(guild_id)
    return store.one('SELECT * FROM bc_club_format WHERE guild_id=?', (guild_id,)) or dict(
        guild_id=guild_id, body=DEFAULT_BODY, essay_lead_hours=24, revision=0,
        actor_id=None, updated_at=None, schedule_ids='[]')


def save_format(store, guild_id, actor_id, body, essay_lead_hours, expected_revision, schedule_ids=None):
    body = checked_text(body, 'Формат клуба', 1400)
    if type(essay_lead_hours) is not int or not 1 <= essay_lead_hours <= 168:
        raise ClubError('Срок эссе: от 1 до 168 часов до обсуждения.')
    settings = store.settings(guild_id)
    if schedule_ids is not None and (not isinstance(schedule_ids, list) or len(schedule_ids) > 10
            or any(type(value) is not int or value <= 0 for value in schedule_ids)
            or len(set(schedule_ids)) != len(schedule_ids)):
        raise ClubError('Расписание: до 10 разных событий этого сервера.')
    with store.tx() as db:
        row = db.execute('SELECT * FROM bc_club_format WHERE guild_id=?', (guild_id,)).fetchone()
        old = dict(row) if row else dict(body=DEFAULT_BODY, essay_lead_hours=24, revision=0, schedule_ids='[]')
        if old['revision'] != expected_revision:
            raise ClubError('Формат уже изменён. Откройте редактирование заново.')
        events = json.dumps(schedule_ids) if schedule_ids is not None else old['schedule_ids']
        if old['body'] == body and old['essay_lead_hours'] == essay_lead_hours and old['schedule_ids'] == events:
            return
        revision, timestamp = old['revision'] + 1, store.clock()
        db.execute('''INSERT INTO bc_club_format VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(guild_id) DO UPDATE SET body=excluded.body,
          essay_lead_hours=excluded.essay_lead_hours,revision=excluded.revision,
          actor_id=excluded.actor_id,updated_at=excluded.updated_at,schedule_ids=excluded.schedule_ids''',
          (guild_id, body, essay_lead_hours, revision, actor_id, timestamp, events))
        db.execute('''INSERT INTO bc_format_audit
          (guild_id,revision,actor_id,created_at,old_body,new_body,old_hours,new_hours,old_events,new_events)
          VALUES(?,?,?,?,?,?,?,?,?,?)''', (guild_id, revision, actor_id, timestamp,
          old['body'], body, old['essay_lead_hours'], essay_lead_hours, old['schedule_ids'], events))
        for book in db.execute('SELECT id FROM bc_books WHERE guild_id=?', (guild_id,)).fetchall():
            store._essay_schedule(db, book['id'], settings)


def format_content(store, guild_id):
    value = get_format(store, guild_id)
    text = ('## Как устроен книжный клуб\n' + value['body']
            + f'\n\n**Эссе — за {value["essay_lead_hours"]} ч до встречи «Обсуждение эссе».** '
              'При переносе встречи срок сдвигается вместе с ней.')
    if value['actor_id']:
        text += f'\n\nИзменил(а) <@{value["actor_id"]}> · <t:{value["updated_at"]}:f>'
    return text
