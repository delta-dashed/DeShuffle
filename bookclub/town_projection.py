"""Separate town topics with one native forum pin and durable publication IDs."""
from __future__ import annotations

import hashlib
import logging

import discord

from .town_render import SECTION_TITLES, town_guide, town_sections

log = logging.getLogger(__name__)
TOPICS = tuple(SECTION_TITLES.items())
GUIDE_TITLE = 'Путеводитель книжного клуба'


def _url(guild_id, pub):
    return f'https://discord.com/channels/{guild_id}/{pub["channel_id"]}/{pub["message_id"]}'


def _view(service, guild_id, links, *, actions=False):
    view = service.catalog_view_factory(guild_id) if actions and service.catalog_view_factory else discord.ui.View(timeout=None)
    for label, url in links:
        view.add_item(discord.ui.Button(label=label, url=url, row=1 if actions else 0))
    return view


async def _publish(service, guild, key, forum_id, name, contents, view, *, verify):
    # Fast book saves do no HTTP reads for unchanged sections. A normal full
    # refresh and a new process still validate every root via paged_post.
    cache = service.town_cache
    signature = hashlib.sha256(repr((forum_id, name, contents, view.to_components())).encode()).hexdigest()
    pub = service.store.publication(key)
    identity = (pub['channel_id'], pub['message_id'], pub['content_hash']) if pub else None
    if not verify and identity and identity[1] and cache.get(key) == (signature, identity):
        return pub
    pub = await service.paged_post(guild, key, forum_id, name, contents, view=view, inline_first=True)
    thread = await service.channel(guild, pub['channel_id'], discord.Thread)
    await service.forum_tags.sync_thread_tags(guild, thread, ['catalog'])
    cache[key] = (signature, (pub['channel_id'], pub['message_id'], pub['content_hash']))
    return pub


async def _pin_guide(service, guild, forum_id, guide, *, verify):
    if not verify and guild.id in service.town_pin_checked:
        return
    thread = await service.channel(guild, guide['channel_id'], discord.Thread)
    if thread.flags.pinned:
        service.town_pin_checked.add(guild.id)
        service.town_pin_warning.pop(guild.id, None)
        return
    forum = await service.channel(guild, forum_id, discord.ForumChannel)
    warning = None
    if not forum.permissions_for(guild.me).manage_threads:
        warning = 'Manage Threads is required to pin the town guide'
    else:
        threads = await guild.active_threads()
        threads.extend([t async for t in forum.archived_threads(limit=None)])
        if any(t.parent_id == forum_id and t.id != thread.id and t.flags.pinned for t in threads):
            warning = 'Another forum topic is pinned; town guide remains available without replacing it'
        else:
            try:
                await thread.edit(pinned=True)
            except discord.Forbidden:
                warning = 'Discord denied pinning the town guide'
    service.town_pin_checked.add(guild.id)
    if warning:
        if service.town_pin_warning.get(guild.id) != warning:
            log.warning('%s guild=%s forum=%s', warning, guild.id, forum_id)
        service.town_pin_warning[guild.id] = warning
    else:
        service.town_pin_warning.pop(guild.id, None)


async def refresh_town(service, guild, *, verify=False):
    """Called under the existing publication lock; never starts another sender."""
    store = service.store
    settings = store.settings(guild.id)
    if not settings['published']:
        return
    forum_id, key = settings['books'], f'catalog:{guild.id}'
    guide = store.publication(key)
    needs_root = not guide or not guide['message_id']
    if not needs_root:
        try:
            thread = await service.channel(guild, guide['channel_id'])
            needs_root = not isinstance(thread, discord.Thread) or thread.parent_id != forum_id
        except discord.NotFound:
            needs_root = True
    if needs_root:
        # Stable provisional content is recoverable even if Discord loses the
        # create response. The old catalog is reused when it already exists.
        guide = await service.paged_post(guild, key, forum_id, GUIDE_TITLE,
            ['**Путеводитель книжного клуба**\nПодготавливаем разделы…'],
            view=_view(service, guild.id, [], actions=True), inline_first=True)
        service.town_pin_checked.discard(guild.id)

    old_guide_url = _url(guild.id, guide)
    sections = {}
    for purpose, title in TOPICS:
        # A saved edit can overtake us at each safe publication boundary.
        content = town_sections(store, guild.id)[purpose]
        sections[purpose] = await _publish(service, guild, f'{key}:{purpose}', forum_id, title, content,
            _view(service, guild.id, [('Путеводитель', old_guide_url)], actions=purpose == 'plan'), verify=verify)
        await service.book_updates.drain(guild)
    urls = {purpose: _url(guild.id, pub) for purpose, pub in sections.items()}
    guide = await _publish(service, guild, key, forum_id, GUIDE_TITLE, town_guide(store, guild.id, urls),
        _view(service, guild.id, [(title, urls[purpose]) for purpose, title in TOPICS], actions=True), verify=verify)
    guide_url = _url(guild.id, guide)
    if guide_url != old_guide_url:
        # A deleted starter discovered by paged_post changes the root ID.
        # Repair all back links in this pass, preserving each section's ID.
        service.town_pin_checked.discard(guild.id)
        for purpose, title in TOPICS:
            sections[purpose] = await _publish(service, guild, f'{key}:{purpose}', forum_id, title, town_sections(store, guild.id)[purpose],
                _view(service, guild.id, [('Путеводитель', guide_url)], actions=purpose == 'plan'), verify=False)
        repaired_urls = {purpose: _url(guild.id, pub) for purpose, pub in sections.items()}
        if repaired_urls != urls:
            guide = await _publish(service, guild, key, forum_id, GUIDE_TITLE,
                town_guide(store, guild.id, repaired_urls),
                _view(service, guild.id, [(title, repaired_urls[purpose]) for purpose, title in TOPICS], actions=True),
                verify=False)
    await _pin_guide(service, guild, forum_id, guide, verify=verify)
    service.book_updates.failures.pop((guild.id, 'catalog'), None)
    return guide
