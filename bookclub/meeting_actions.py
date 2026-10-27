"""Organizer meeting actions shared by commands and private button controls.

Discord remains the calendar. Every operation checks current membership and the
native event before changing a meeting; failed event creation is never retried
without reconciling the durable request marker first.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord

from .store import ClubError, checked_text, parse_time
from .event_delivery import event_description, find_draft_events


async def _organizer(service, guild, actor_id):
    _, organizer = await service.actor(guild, actor_id)
    if not organizer:
        raise ClubError('Управление встречами доступно организатору.')


def _times(service, guild, date, minutes):
    start = parse_time(date, service.store.settings(guild.id)['timezone'])
    if type(minutes) is not int or not 1 <= minutes <= 1440 or start <= service.store.clock():
        raise ClubError('Нужны будущая дата и длительность от 1 до 1440 минут.')
    return start, start + minutes * 60


def _event_matches(event, guild, meeting):
    return (getattr(event, 'guild_id', None) == guild.id
            and getattr(event, 'id', None) == meeting['event_id']
            and getattr(event, 'entity_type', None) == discord.EntityType.voice)


async def _current(service, guild, meeting_id, expected_revision):
    meeting = service.store.meeting(guild.id, meeting_id)
    service.store.require_active_book(guild.id, meeting['book_id'])
    if not meeting['event_id']:
        raise ClubError('Создание события не подтверждено. Используйте /club recover_event.')
    try:
        event = await guild.fetch_scheduled_event(meeting['event_id'])
    except discord.NotFound as exc:
        await service.sync_one(guild, meeting)
        raise ClubError('Событие больше недоступно. Откройте список встреч заново.') from exc
    if not _event_matches(event, guild, meeting):
        raise ClubError('Нужно связанное голосовое событие этого сервера.')
    service.sync(guild, meeting, event)
    meeting = service.store.meeting(guild.id, meeting_id)
    if expected_revision is not None and meeting['revision'] != expected_revision:
        raise ClubError('Встреча изменилась. Откройте управление встречей заново.')
    return meeting, event


async def _recover_draft(service, guild, meeting):
    events = await guild.fetch_scheduled_events()
    matches = find_draft_events(service.store, guild, meeting, events, service.bot.user.id)
    if len(matches) != 1:
        raise ClubError('Предыдущая попытка создания не подтверждена. Используйте /club recover_event; повторное событие не создано.')
    service.sync(guild, meeting, matches[0])
    return service.store.meeting(guild.id, meeting['id'])


async def _change_event(service, guild, meeting, change, confirmed):
    """Recover a lost response only when Discord confirms the requested change."""
    failure = None
    response = None
    try:
        response = await change()
    except (discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
        failure = exc
    try:
        event = await guild.fetch_scheduled_event(meeting['event_id'])
    except discord.NotFound as exc:
        # Discord can remove cancelled events from REST immediately. A native
        # successful cancellation response is still authoritative evidence.
        if (response is not None and _event_matches(response, guild, meeting)
                and response.status == discord.EventStatus.cancelled and confirmed(response)):
            return response
        raise ClubError('Не удалось подтвердить изменение события. Откройте управление заново и проверьте Discord.') from (failure or exc)
    except (discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
        transient = (not isinstance(exc, discord.HTTPException) or 500 <= exc.status <= 599)
        # A successful native mutation response already confirms its fields.
        # A subsequent transient GET failure must not leave boundary metadata
        # and plan readiness behind the acknowledged Discord edit.
        if transient and _event_matches(response, guild, meeting) and confirmed(response):
            return response
        raise ClubError('Не удалось подтвердить изменение события. Откройте управление заново и проверьте Discord.') from (failure or exc)
    if not _event_matches(event, guild, meeting) or not confirmed(event):
        # An unrelated concurrent native edit must still become our cache, but
        # never present the requested mutation as successful.
        if _event_matches(event, guild, meeting):
            service.sync(guild, meeting, event)
        raise ClubError('Discord не подтвердил изменение. Откройте управление встречей заново.') from failure
    return event


async def create_meeting(service, guild, actor_id, book_id, *, name, date, minutes, part, chapter, request_key,
                         plan_kind='reading'):
    if guild is None:
        raise ClubError('Управление встречами доступно только на сервере.')
    async with service.locks[guild.id]:
        await _organizer(service, guild, actor_id)
        service.store.require_active_book(guild.id, book_id)
        if plan_kind not in {'reading', 'essay'}:
            raise ClubError('Выберите тип встречи: по книге или обсуждение эссе.')
        name = checked_text(name, 'Название встречи', 100)
        part = checked_text(part, 'Часть', 200)
        chapter = checked_text(chapter, 'Последняя глава', 250)
        request_key = checked_text(str(request_key), 'Запрос создания', 200)
        start, end = _times(service, guild, date, minutes)
        existing = service.store.one('SELECT * FROM bc_meetings WHERE guild_id=? AND request_key=?',
                                     (guild.id, request_key))
        if existing:
            if existing['book_id'] != book_id:
                raise ClubError('Этот запрос уже относится к другой книге. Откройте управление заново.')
            if existing['event_id']:
                result, _ = await _current(service, guild, existing['id'], None)
            else:
                result = await _recover_draft(service, guild, existing)
        else:
            draft = service.store.draft_meeting(guild.id, book_id, name, part, chapter, request_key, plan_kind=plan_kind)
            try:
                await service.create_event(guild, draft, start, end)
            except (discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
                try:
                    result = await _recover_draft(service, guild, draft)
                except (ClubError, discord.HTTPException, OSError, asyncio.TimeoutError):
                    raise ClubError('Не удалось подтвердить создание события. Используйте /club recover_event; не создавайте встречу повторно.') from exc
            else:
                result, _ = await _current(service, guild, draft['id'], None)
        await service.refresh(guild)
        return result


async def move_meeting(service, guild, actor_id, meeting_id, *, date, minutes, expected_revision=None):
    if guild is None:
        raise ClubError('Управление встречами доступно только на сервере.')
    async with service.locks[guild.id]:
        await _organizer(service, guild, actor_id)
        start, end = _times(service, guild, date, minutes)
        meeting, event = await _current(service, guild, meeting_id, expected_revision)
        if event.status != discord.EventStatus.scheduled:
            raise ClubError('Можно переносить только ещё не начавшуюся встречу.')
        matches = lambda current: (current.status == discord.EventStatus.scheduled
                                    and int(current.start_time.timestamp()) == start
                                    and current.end_time is not None
                                    and int(current.end_time.timestamp()) == end)
        if not matches(event):
            event = await _change_event(service, guild, meeting, lambda: event.edit(
                start_time=datetime.fromtimestamp(start, timezone.utc),
                end_time=datetime.fromtimestamp(end, timezone.utc)), matches)
            service.sync(guild, meeting, event)
        await service.refresh(guild)
        return service.store.meeting(guild.id, meeting_id)


async def edit_meeting(service, guild, actor_id, meeting_id, *, name=None, part, chapter, expected_revision=None):
    if guild is None:
        raise ClubError('Управление встречами доступно только на сервере.')
    async with service.locks[guild.id]:
        await _organizer(service, guild, actor_id)
        part = checked_text(part, 'Часть', 200)
        chapter = checked_text(chapter, 'Последняя глава', 250)
        if name is not None:
            name = checked_text(name, 'Название встречи', 100)
        meeting, event = await _current(service, guild, meeting_id, expected_revision)
        if event.status not in (discord.EventStatus.scheduled, discord.EventStatus.active):
            raise ClubError('Изменять можно только будущую или текущую встречу.')
        boundary_changed = part != meeting['part'] or chapter != meeting['chapter']
        fields = {}
        if boundary_changed:
            fields['description'] = event_description(service.store, guild.id, {**meeting, 'part': part, 'chapter': chapter})
        if name is not None and name != meeting['name']:
            fields['name'] = name
        if fields:
            event = await _change_event(service, guild, meeting, lambda: event.edit(**fields),
                lambda current: current.status in (discord.EventStatus.scheduled, discord.EventStatus.active)
                and all(getattr(current, key) == value for key, value in fields.items()))
            service.sync(guild, meeting, event)
            if boundary_changed:
                settings = service.store.settings(guild.id)
                with service.store.tx() as db:
                    current = service.store._get(db, 'bc_meetings', guild.id, meeting_id)
                    service.store._active_book(db, guild.id, current['book_id'])
                    db.execute('UPDATE bc_meetings SET part=?,chapter=?,revision=revision+1 WHERE id=?',
                               (part, chapter, meeting_id))
                    db.execute('UPDATE bc_plans SET ready=0,version=version+1 WHERE meeting_id=? AND generation=?',
                               (meeting_id, current['plan_generation']))
                    service.store._schedule(db, meeting_id, settings)
        await service.refresh(guild)
        return service.store.meeting(guild.id, meeting_id)


async def cancel_meeting(service, guild, actor_id, meeting_id, *, expected_revision=None):
    if guild is None:
        raise ClubError('Управление встречами доступно только на сервере.')
    async with service.locks[guild.id]:
        await _organizer(service, guild, actor_id)
        meeting, event = await _current(service, guild, meeting_id, expected_revision)
        if event.status != discord.EventStatus.scheduled:
            raise ClubError('Отменять можно только ещё не начавшуюся встречу.')
        event = await _change_event(service, guild, meeting, event.cancel,
                                    lambda current: current.status == discord.EventStatus.cancelled)
        service.sync(guild, meeting, event)
        await service.refresh(guild)
        return service.store.meeting(guild.id, meeting_id)
