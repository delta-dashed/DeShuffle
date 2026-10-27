"""Remove only verified obsolete bot navigation notices, never book content."""
from __future__ import annotations

import logging
import re

import discord

log = logging.getLogger(__name__)
OBSOLETE_NOTICES = frozenset({
    'Раздел обновлён. Актуальные материалы и переходы — в начале темы.',
    'Каталог обновлён. Текущая книга и очередь — в начале темы.',
    'Эта страница больше не нужна. Актуальная карточка — в начале темы.',
})


def _only_root_links(message, root_url):
    for row in message.components:
        value = row.to_dict()
        if value.get('type') != 1:
            return False
        for button in value.get('components', []):
            if (button.get('type') != 2 or button.get('style') != 5
                    or button.get('url') != root_url
                    or button.get('label') not in {'К началу', 'Текущая книга'}
                    or button.get('custom_id')):
                return False
    return True


async def cleanup_town_pages(service, guild, rootkey, active_page_count):
    """Delete obsolete generated notices after a root's current pages are ready.

    The caller owns the guild publication lock. The publication stays bound
    until DELETE succeeds or a GET proves absence, including after lost replies.
    Changed messages remain untouched and are reported without their content.
    """
    if not re.fullmatch(rf'catalog:{guild.id}(?::(?:current|plan|archive))?', rootkey):
        raise ValueError('Cleanup is restricted to this guild\'s town navigation')
    if type(active_page_count) is not int or active_page_count < 0:
        raise ValueError('Active page count must be a nonnegative integer')
    store = service.store
    root = store.publication(rootkey)
    if not root or root['guild_id'] != guild.id or not root['message_id']:
        return 0
    try:
        thread = await service.channel(guild, root['channel_id'], discord.Thread)
    except discord.NotFound:
        return 0
    if thread.guild.id != guild.id or thread.parent_id != store.settings(guild.id)['books']:
        return 0
    root_url = f'https://discord.com/channels/{guild.id}/{root["channel_id"]}/{root["message_id"]}'
    removed = 0
    for pub in store.rows('SELECT * FROM bc_publications WHERE guild_id=? AND key LIKE ?',
                          (guild.id, rootkey + ':page:%')):
        suffix = pub['key'][len(rootkey + ':page:'):]
        if not suffix.isdecimal() or int(suffix) <= active_page_count:
            continue
        if (not pub['message_id'] or pub['channel_id'] != root['channel_id']
                or pub['message_id'] == root['message_id']):
            continue
        if store.one('''SELECT key FROM bc_publications WHERE guild_id=?
          AND message_id=? AND key<>? LIMIT 1''', (guild.id, pub['message_id'], pub['key'])):
            log.warning('Obsolete town page has another binding guild=%s message=%s',
                        guild.id, pub['message_id'])
            continue
        try:
            message = await thread.fetch_message(pub['message_id'])
        except discord.NotFound:
            store.forget_publication(pub['key'])
            removed += 1
            continue
        embeds = getattr(message, 'embeds', ())
        if (message.channel.id != thread.id or not service.owns_starter(message)
                or message.attachments or message.content not in OBSOLETE_NOTICES
                or (isinstance(embeds, (list, tuple)) and embeds)
                or not _only_root_links(message, root_url)):
            log.warning('Obsolete town page retained after manual change guild=%s message=%s',
                        guild.id, pub['message_id'])
            continue
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning('Obsolete town page needs delete permission guild=%s message=%s',
                        guild.id, pub['message_id'])
            continue
        except (discord.HTTPException, OSError, TimeoutError):
            # A lost DELETE response may already have removed the message.
            # Never forget a binding while the resource still exists/unknown.
            try:
                await thread.fetch_message(pub['message_id'])
            except discord.NotFound:
                pass
            else:
                raise
        store.forget_publication(pub['key'])
        removed += 1
    return removed
