"""Public projections. Deliberately never accepts private plan text."""
from datetime import datetime
from zoneinfo import ZoneInfo
from .store import STATUSES


def safe(value):
    return str(value).replace('@', '@\u200b').replace('`', 'ˋ').replace('*', '∗').replace('_', '＿').replace('[', '［').replace(']', '］')


def pages(lines, limit=1750):
    result, current = [], ''
    for line in lines:
        while len(line) > limit:
            if current:
                result.append(current)
                current = ''
            result.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            result.append(current)
            current = ''
        current += ('\n' if current else '') + line
    if current or not result:
        result.append(current or 'Пока пусто.')
    return result


def time_label(timestamp, zone):
    if timestamp is None:
        return 'не задано'
    return datetime.fromtimestamp(timestamp, ZoneInfo(zone)).strftime('%d.%m.%Y %H:%M') + f' ({zone})'


def book_url(store, book):
    pub = store.publication(f'book:{book["id"]}')
    if pub and pub['message_id']:
        return f'https://discord.com/channels/{book["guild_id"]}/{pub["channel_id"]}/{pub["message_id"]}'
    return None


def meeting_lines(store, meeting, settings):
    m = meeting
    host = 'ведущий не выбран'
    if m['host_state'] == 'pending':
        host = f'<@{m["host_id"]}> — ожидаем подтверждения до {time_label(m["offer_until"], settings["timezone"])}'
    elif m['host_state'] == 'confirmed':
        plan = store.one('SELECT ready FROM bc_plans WHERE meeting_id=? AND generation=?', (m['id'], m['plan_generation']))
        host = f'<@{m["host_id"]}> — ' + ('План готов' if plan and plan['ready'] else 'Готовится · план не отмечен готовым')
    elif m['exhausted']:
        host += ' · нет доступных кандидатов; нужен организатор'
    status = {'draft': 'ещё не создана', 'scheduled': 'запланирована', 'active': 'идёт', 'completed': 'завершена', 'cancelled': 'отменена', 'unsupported': 'нужен голосовой канал в событии; напоминания приостановлены'}.get(m['status'], m['status'])
    if m['status'] == 'cancelled' and not m.get('event_status_confirmed', 1):
        status = 'недоступна · отмена не подтверждена'
    lines = [f'**{safe(m["name"])}** · {status}',
             f'Когда: {time_label(m["start"], settings["timezone"])}',
             f'Окончание: {time_label(m["end"], settings["timezone"])}',
             f'Читаем: {safe(m["part"])}; до главы {safe(m["chapter"])} включительно.',
             f'Ведущий: {host}']
    kind = {'reading': 'по книге', 'essay': 'обсуждение эссе'}.get(m.get('plan_kind'), 'нужно выбрать организатору')
    lines.append(f'Тип встречи в плане: {kind}.')
    if m['event_id']:
        lines.append(f'[Событие Discord](https://discord.com/events/{m["guild_id"]}/{m["event_id"]}) · <#{m["voice_id"]}>')
    participants = store.meeting_participants(m['id'])
    lines.append(f'Участников встречи: {len(participants)} (состав чтения за вычетом отметивших отсутствие).')
    url = book_url(store, {'id': m['book_id'], 'guild_id': m['guild_id']})
    if url:
        lines.append(f'[Книга и эссе участников]({url})')
    return lines


def essay_lines(store, book):
    """All submitted works, shared by the book card and the essay button."""
    essays = store.essays(book['id'])
    lines = ['**Эссе участников**']
    lines.extend(f'<@{e["author_id"]}> · [{safe(e["title"])}]({e["url"]})' for e in essays)
    if not essays:
        lines.append('Пока нет опубликованных работ.')
    return lines


def book_pages(store, book, settings):
    b = book
    lines = [f'**{safe(b["title"])} · {safe(b["author"])}**', f'{STATUSES[b["status"]]} · очередь: {b["position"]}',
             f'Срок эссе: {time_label(b["deadline"], settings["timezone"])}',
             'Нажмите «Добавить своё эссе», чтобы открыть свой пост, или «Эссе участников», чтобы прочитать все работы.']
    count = b.get('reading_meetings')
    lines.append(f'План: {count} встреч по книге + 1 разбор эссе = {count + 1} всего.' if count is not None else
                 'План встреч пока не задан: N встреч по книге + 1 разбор эссе.')
    lines.append('Организатору: «Управление книгой» — статус и план; «Встречи» — даты и переносы.')
    lines.extend(essay_lines(store, b))
    lines.extend(['Материалы:', b['materials'] or 'Пока не добавлены.',
             'Камеры включены, микрофоны исправны. Спойлеры дальше границы чтения скрываем через ||текст||.',
             'Участники чтения: ' + (', '.join(f'<@{p["user_id"]}>' for p in store.participants(b['id'])) or 'пока нет'),
             '**Встречи**'])
    meetings = store.rows('SELECT * FROM bc_meetings WHERE book_id=? ORDER BY start,id', (b['id'],))
    for m in meetings:
        lines.extend(meeting_lines(store, m, settings))
    return pages(lines)


def catalog_pages(store, guild_id):
    lines = ['**Каталог книжного клуба**', 'Порядок чтения задаёт организатор; ответы в обсуждениях его не меняют.',
             '«Добавить книгу» создаст карточку и отдельный пост для обсуждения. «Загрузить список» — вставить несколько книг; '
             'для файла используйте `/club library import`.']
    for b in store.books(guild_id):
        url = book_url(store, b)
        title = f'{safe(b["title"])} · {safe(b["author"])}'
        lines.append(f'{b["position"]}. ' + (f'[{title}]({url})' if url else title) + f' — {STATUSES[b["status"]]}')
    return pages(lines)


def news_content(store, guild_id, settings):
    return ('**Вестник клуба**\nОрганизационные объявления: даты встреч, переносы, отмены и другие изменения. '
            'Организаторы публикуют их здесь вручную.\n'
            f'Актуальные даты и границы чтения — в карточках книг в <#{settings["books"]}> и событиях сервера. '
            'Бот не дублирует сюда обычное расписание.\n'
            f'Для флуда и общения — <#{settings["chat"]}>.')
