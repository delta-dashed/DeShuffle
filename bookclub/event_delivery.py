"""Recover native event creation by its saved request without public IDs."""
import json
import discord


def event_description(store, guild_id, meeting):
    from .render import book_url, safe
    text = f'{safe(meeting["part"])}; до главы {safe(meeting["chapter"])} включительно.'
    url = book_url(store, dict(id=meeting['book_id'], guild_id=guild_id))
    return text + (f'\n[Книга и обсуждение]({url})' if url else '')


def find_draft_events(store, guild, meeting, events, bot_id):
    row = store.one('SELECT * FROM bc_event_intents WHERE meeting_id=?', (meeting['id'],))
    legacy = f'[bookclub:{meeting["id"]}]'
    if row is None:
        return [e for e in events if e.guild_id == guild.id and e.entity_type == discord.EntityType.voice
                and legacy in (e.description or '').splitlines()]
    expected = json.loads(row['payload'])
    return [e for e in events if e.guild_id == guild.id and e.entity_type == discord.EntityType.voice
            and e.id > row['after_id'] and getattr(e, 'creator_id', None) == bot_id
            and e.name == expected['name'] and (e.description or '') == expected['description']
            and e.channel_id == expected['voice_id']
            and int(e.start_time.timestamp()) == expected['start']
            and (int(e.end_time.timestamp()) if e.end_time else None) == expected['end']]
