"""Small public views for the town guide and its three permanent topics.

Only public book, essay and event fields are read. Private host plans are never
part of these projections, even when the viewer is an organizer.
"""
from .render import book_url, pages, plan_label, safe, time_label


SECTION_TITLES = {
    'current': 'Текущая книга',
    'plan': 'План чтения',
    'archive': 'Архив прочитанного',
}


def _label(value):
    return safe(' '.join(str(value).split()))


def _book_link(store, book):
    label = f'{_label(book["title"])} · {_label(book["author"])}'
    url = book_url(store, book)
    return f'[{label}]({url})' if url else label


def _section_pages(section, lines):
    title = SECTION_TITLES[section]
    bodies = pages(lines, limit=1750 - len(title) - 48)
    return [f'## {title}' + (f' · {index}/{len(bodies)}' if len(bodies) > 1 else '')
            + '\n' + body for index, body in enumerate(bodies, 1)]


def _current_lines(store, current):
    if not current:
        return ['Текущая книга ещё не выбрана. Организатор выбирает её из очереди в плане клуба.']
    lines = []
    for book in current:
        lines.extend([f'### {_book_link(store, book)}', '**Читаем**'])
        url = book_url(store, book)
        if url:
            lines.append(f'[Материалы, встречи и эссе участников]({url})')
        else:
            lines.append('Карточка книги ещё готовится.')
        lines.append(f'Опубликовано эссе: **{len(store.essays(book["id"]))}**.')
        lines.append('Откройте карточку книги, чтобы добавить своё эссе или управлять книгой. '
                     'Порядок чтения и ближайшие встречи — в теме «План чтения».')
    return lines


def _plan_lines(store, guild_id, current, queued, proposed, settings):
    lines, shown_events = ['### План текущей книги'], set()
    if not current:
        lines.append('План текущего чтения появится после выбора книги.')
    for book in current:
        lines.extend([_book_link(store, book), f'**{plan_label(book)}**.'])
        meetings = store.rows("""SELECT * FROM bc_meetings WHERE guild_id=? AND book_id=?
          AND status IN ('scheduled','active') ORDER BY start,id""", (guild_id, book['id']))
        if not meetings:
            lines.append('Предстоящие встречи по книге ещё не назначены.')
        for meeting in meetings:
            title = _label(meeting['name'])
            if meeting['event_id']:
                shown_events.add(meeting['event_id'])
                title = f'[{title}](https://discord.com/events/{guild_id}/{meeting["event_id"]})'
            status = 'идёт сейчас' if meeting['status'] == 'active' else 'запланирована'
            when = time_label(meeting['start'], settings['timezone']) if meeting['start'] is not None else 'дата не назначена'
            lines.append(f'• {title} · {status} · {when}')
            if meeting.get('plan_kind') == 'essay':
                lines.append('  Обсуждаем эссе по книге.')
            else:
                lines.append(f'  Читаем: {_label(meeting["part"])} · до главы {_label(meeting["chapter"])} включительно.')
    schedule = store.rows("""SELECT * FROM bc_schedule_events WHERE guild_id=?
      AND status IN ('scheduled','active') ORDER BY start,event_id""", (guild_id,))
    schedule = [event for event in schedule if event['event_id'] not in shown_events]
    if schedule:
        lines.append('\n### Ближайшие синки клуба')
        for event in schedule:
            when = time_label(event['start'], settings['timezone']) if event['start'] is not None else 'дата не назначена'
            status = ' · идёт сейчас' if event['status'] == 'active' else ''
            lines.append(f'• [{_label(event["name"])}](https://discord.com/events/{guild_id}/{event["event_id"]})'
                         f' · {when}{status}')
    lines.extend(['\n### Очередь чтения', 'Читаем по порядку сверху вниз. Встречи каждой книги — в её карточке.'])
    lines.extend(f'**{index}.** {_book_link(store, book)}' for index, book in enumerate(queued, 1))
    if not queued:
        lines.append('Очередь пока пуста.')
    lines.append('\n### Предложения · ещё не в очереди')
    lines.extend(f'• {_book_link(store, book)}' for book in proposed)
    if not proposed:
        lines.append('Предложений пока нет.')
    return lines


def town_sections(store, guild_id):
    """Return complete, independently paginated current/plan/archive topics."""
    books, settings = store.books(guild_id), store.settings(guild_id)
    current = [book for book in books if book['status'] == 'reading']
    queued = [book for book in books if book['status'] == 'queued']
    proposed = [book for book in books if book['status'] == 'proposed']
    archive = [book for book in books if book['status'] == 'read']
    archived_lines = ['Прочитанные книги, их материалы и эссе участников.']
    archived_lines.extend(f'• {_book_link(store, book)} · эссе: {len(store.essays(book["id"]))}' for book in archive)
    if not archive:
        archived_lines.append('Прочитанных книг пока нет.')
    return {
        'current': _section_pages('current', _current_lines(store, current)),
        'plan': _section_pages('plan', _plan_lines(store, guild_id, current, queued, proposed, settings)),
        'archive': _section_pages('archive', archived_lines),
    }


def town_guide(store, guild_id, section_urls):
    """The short pinned entry point; topic titles may be renamed independently."""
    books = store.books(guild_id)
    current = next((book for book in books if book['status'] == 'reading'), None)
    lines = ['## Книжный клуб · навигация',
             ('Сейчас читаем: ' + _book_link(store, current)) if current else 'Текущая книга ещё не выбрана.']
    descriptions = {
        'current': 'текущая книга, материалы и эссе',
        'plan': 'встречи, границы чтения, очередь и предложения',
        'archive': 'прочитанные книги и работы участников',
    }
    for key, title in SECTION_TITLES.items():
        url = section_urls.get(key)
        label = f'[{title}]({url})' if url else title
        lines.append(f'• {label} — {descriptions[key]}.' + ('' if url else ' Тема ещё готовится.'))
    lines.append('\nЧтобы добавить своё эссе, откройте карточку нужной книги. '
                 'Организаторы управляют книгами и очередью через кнопки каталога.')
    return pages(lines)
