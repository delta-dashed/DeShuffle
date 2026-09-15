"""Public projections. Deliberately never accepts private plan text."""
from datetime import datetime
import re
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo
from .store import STATUSES
from .club_format import reading_count


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


def material_links(value):
    """Keep authored Markdown links and label bare file URLs without changing stored data."""
    pattern = r'\[[^\]\n]+\]\((?:<https?://[^>\n]+>|https?://[^\s)]+)\)|https?://[^\s<>]+'
    def replace(match):
        url = match.group()
        if url.startswith('['):
            return url
        clean = url.rstrip('.,;!')
        suffix = url[len(clean):]
        try:
            path = urlsplit(clean).path.lower()
        except ValueError:
            return url
        label = 'Читать PDF' if path.endswith('.pdf') else 'Скачать EPUB' if path.endswith('.epub') else 'Открыть материал'
        destination = quote(clean, safe='/:?=&%#+@!$\'*,;~.-_')
        return f'[{label}](<{destination}>){suffix}'
    return re.sub(pattern, replace, value or '')


def plan_label(book):
    count = reading_count(book)
    word = 'встреч' if 11 <= count % 100 <= 14 else 'встреча' if count % 10 == 1 else 'встречи' if count % 10 in (2, 3, 4) else 'встреч'
    return f'{count} {word} по книге + обсуждение эссе'


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
    if participants:
        lines.append(f'Участников встречи: {len(participants)}.')
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
    lines = [f'## {safe(b["title"])}', f'{safe(b["author"])} · **{STATUSES[b["status"]]}**',
             f'\nПлан: **{plan_label(b)}**.']
    lines.extend(essay_lines(store, b))
    if b['materials']:
        lines.extend(['\n**Материалы:**', material_links(b['materials'])])
    meetings = store.rows('SELECT * FROM bc_meetings WHERE book_id=? ORDER BY start,id', (b['id'],))
    schedule = store.rows("""SELECT * FROM bc_schedule_events WHERE guild_id=?
      AND status IN ('scheduled','active') ORDER BY start,event_id""", (b['guild_id'],)) if b['status'] == 'reading' else []
    if any(m['status'] != 'cancelled' for m in meetings) or not schedule:
        lines.append('\n**Встречи**')
    for m in meetings:
        if m['status'] == 'cancelled':
            continue
        title = safe(m['name'])
        if m['event_id']:
            title = f'[{title}](https://discord.com/events/{m["guild_id"]}/{m["event_id"]})'
        lines.append(f'• {title} · {time_label(m["start"], settings["timezone"])}')
        if m.get('plan_kind') != 'essay':
            lines.append(f'  {safe(m["part"])} · до главы {safe(m["chapter"])}')
    if not any(m['status'] != 'cancelled' for m in meetings) and not schedule:
        lines.append('Даты ещё не назначены.')
    if schedule:
        lines.append('\n**Ближайшие синки клуба**')
        for event in schedule:
            lines.append(f'• [{safe(event["name"])}](https://discord.com/events/{b["guild_id"]}/{event["event_id"]})'
                         f' · {time_label(event["start"], settings["timezone"])}')
    return pages(lines)


def catalog_pages(store, guild_id):
    books = store.books(guild_id)
    def item(b):
        url = book_url(store, b)
        title = f'{safe(b["title"])} · {safe(b["author"])}'
        return f'[{title}]({url})' if url else title
    current = [b for b in books if b['status'] == 'reading']
    queued = [b for b in books if b['status'] == 'queued']
    archive = [b for b in books if b['status'] == 'read']
    proposed = [b for b in books if b['status'] == 'proposed']
    lines = ['## Сейчас читаем']
    for b in current:
        lines.extend([f'### {item(b)}', plan_label(b)])
    schedule = store.rows("""SELECT * FROM bc_schedule_events WHERE guild_id=? AND status IN ('scheduled','active')
      ORDER BY start,event_id LIMIT 1""", (guild_id,))
    if schedule:
        event = schedule[0]
        lines.append(f'Ближайший синк: [{safe(event["name"])}](https://discord.com/events/{guild_id}/{event["event_id"]})'
                     f' · <t:{event["start"]}:f>')
    if not current:
        lines.append('Текущая книга ещё не выбрана.')
    lines.extend(['\n## Следующие книги', 'Читаем по порядку сверху вниз.'])
    lines.extend(f'**{i}.** {item(b)}' for i, b in enumerate(queued[:10], 1))
    if not queued:
        lines.append('Очередь пока пуста.')
    result = pages(lines, limit=1400)
    for title, section, numbered in [('Продолжение очереди', queued[10:], True),
                                      ('Архив · прочитано', archive, False),
                                      ('Предложения', proposed, False)]:
        if section:
            result.extend(pages([f'## {title} · {len(section)}'] + [
                (f'**{i}.** ' if numbered else '• ') + item(b)
                for i, b in enumerate(section, 11)], limit=1400))
    return result


def news_content(store, guild_id, settings):
    return ('**Вестник клуба**\nОрганизационные объявления: даты встреч, переносы, отмены и другие изменения. '
            'Организаторы публикуют их здесь вручную.\n'
            f'Актуальные даты и границы чтения — в карточках книг в <#{settings["books"]}> и событиях сервера. '
            'Бот не дублирует сюда обычное расписание.\n'
            f'Для флуда и общения — <#{settings["chat"]}>.')
