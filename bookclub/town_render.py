"""Small public views for the town guide and its three permanent topics.

Only public book, essay and event fields are read. Private host plans are never
part of these projections, even when the viewer is an organizer.
"""
from .render import book_url, material_links, pages, plan_label, safe


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
                     'Расписание — в карточке книги, очередь — в теме «План чтения».')
    return lines


def _plan_lines(store, current, queued, proposed):
    lines = ['### Очередь чтения', 'Читаем по порядку сверху вниз.']
    if current:
        url = book_url(store, current[0])
        lines.append(f'[Встречи и синки — в карточке текущей книги]({url})' if url
                     else 'Встречи и синки — в карточке текущей книги.')
    lines.extend(f'**{index}.** {_book_link(store, book)}' for index, book in enumerate(queued, 1))
    if not queued:
        lines.append('Очередь пока пуста.')
    lines.append('\n### Предложения · ещё не в очереди')
    lines.extend(f'• {_book_link(store, book)}' for book in proposed)
    if not proposed:
        lines.append('Предложений пока нет.')
    return lines


def town_sections(store, guild_id):
    """Return the current card and short introductions for the book lists."""
    books = store.books(guild_id)
    current = [book for book in books if book['status'] == 'reading']
    queued = [book for book in books if book['status'] == 'queued']
    archived_lines = ['Прочитанные книги, их материалы и эссе участников.']
    plan_lines = ['Читаем по порядку сверху вниз.']
    if current:
        url = book_url(store, current[0])
        plan_lines.append(f'[Встречи и синки — в карточке текущей книги]({url})' if url
                          else 'Встречи и синки — в карточке текущей книги.')
    if not queued:
        plan_lines.append('Очередь пока пуста.')
    if not any(book['status'] == 'read' for book in books):
        archived_lines.append('Прочитанных книг пока нет.')
    return {
        'current': _section_pages('current', _current_lines(store, current)),
        'plan': _section_pages('plan', plan_lines),
        'archive': _section_pages('archive', archived_lines),
    }


def plan_entries(store, guild_id):
    """Ordered plain messages; slot numbers preserve the displayed queue order."""
    books = store.books(guild_id)
    queued = [book for book in books if book['status'] == 'queued']
    proposed = [book for book in books if book['status'] == 'proposed']
    lines = [f'{index}. {_book_link(store, book)}' for index, book in enumerate(queued, 1)]
    lines.extend(f'Предложено · {_book_link(store, book)}' for book in proposed)
    return dict(enumerate(lines, 1))


def legacy_plan_pages(store, guild_id):
    """Reproduce the former paginated plan for conservative page migration."""
    books = store.books(guild_id)
    current = [book for book in books if book['status'] == 'reading']
    queued = [book for book in books if book['status'] == 'queued']
    proposed = [book for book in books if book['status'] == 'proposed']
    return _section_pages('plan', _plan_lines(store, current, queued, proposed))


def archive_entries(store, guild_id):
    """One complete, unpaginated message for each visible read book.

    Dict insertion order follows the existing saved book order. Book IDs are
    stable publication keys even if the title, author or essay count changes.
    """
    return {book['id']: f'• {_book_link(store, book)} · эссе: {len(store.essays(book["id"]))}'
            for book in store.books(guild_id) if book['status'] == 'read'}


def legacy_archive_pages(store, guild_id):
    """Reproduce the former raw archive pages for conservative migration.

    The old adapter could append a continuation link to the starter; that
    adapter footer is not part of the renderer's original page contents.
    """
    entries = archive_entries(store, guild_id)
    lines = ['Прочитанные книги, их материалы и эссе участников.', *entries.values()]
    if not entries:
        lines.append('Прочитанных книг пока нет.')
    return _section_pages('archive', lines)


def pinned_title(store, guild_id):
    """The managed forum title follows current reading, within Discord's limit."""
    current = next((book for book in store.books(guild_id) if book['status'] == 'reading'), None)
    return ('Текущая книга · ' + _label(current['title']))[:100] if current else 'Текущая книга'


def town_guide(store, guild_id, section_urls):
    """Put current reading in the sole forum pin, followed by plan and archive."""
    current = next((book for book in store.books(guild_id) if book['status'] == 'reading'), None)
    lines = []
    if current:
        lines.extend([f'## {_label(current["title"])}', _label(current['author']), f'\n{plan_label(current)}.'])
        materials = material_links(current['materials']).strip().replace('@', '@\u200b')
        if materials and len(materials) <= 600:
            lines.extend(['\n**Материалы**', materials])
        elif materials:
            # Never shorten Markdown or a signed URL in the middle. The full
            # original materials stay available in the persistent book card.
            lines.append('Все материалы и файлы — в карточке книги.')
        url = book_url(store, current)
        count = len(store.essays(current['id']))
        if url:
            lines.append(f'\n[Эссе участников — {count} · Карточка книги]({url})')
        else:
            lines.append(f'Опубликовано эссе: **{count}**. Карточка книги ещё готовится.')
    else:
        lines.append('Текущая книга ещё не выбрана. Организатор выбирает её из очереди в плане чтения.')
    return pages(lines)
