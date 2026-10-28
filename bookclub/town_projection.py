"""Separate town topics with one native forum pin and durable publication IDs."""
from __future__ import annotations

import hashlib
import logging

import discord

from .town_render import SECTION_TITLES, pinned_title, town_guide, town_sections
from .store import ClubError

log = logging.getLogger(__name__)
TOPICS = tuple((key, title) for key, title in SECTION_TITLES.items() if key != 'current')


def _url(guild_id, pub):
    return f'https://discord.com/channels/{guild_id}/{pub["channel_id"]}/{pub["message_id"]}'


def _view(service, guild_id, links, *, actions=False):
    factory = getattr(service, 'town_menu_factory', None)
    view = factory(guild_id) if actions and factory else discord.ui.View(timeout=None)
    for label, url in links:
        view.add_item(discord.ui.Button(label=label, url=url, row=1 if actions else 0))
    return view


def _current_view(service, guild_id, urls):
    from .catalog_ui import CatalogGuardedView
    view = CatalogGuardedView(timeout=None)
    book = next((b for b in service.store.books(guild_id) if b['status'] == 'reading'), None)
    if book and service.book_view_factory:
        actions = service.book_view_factory(book)
        for button in list(actions.children):
            if not button.custom_id.endswith((':write', ':own')):
                continue
            actions.remove_item(button)
            button.row = 1
            view.add_item(button)
    menu_factory = getattr(service, 'town_menu_factory', None)
    if menu_factory:
        menu = menu_factory(guild_id)
        for button in list(menu.children):
            menu.remove_item(button)
            button.row = 1
            view.add_item(button)
    for purpose, label in (('plan', 'План чтения'), ('archive', 'Архив')):
        if purpose in urls:
            view.add_item(discord.ui.Button(label=label, url=urls[purpose], row=0))
    return view


async def _recover_legacy_current(service, guild, key):
    publications = service.store.rows('''SELECT * FROM bc_publications WHERE guild_id=?
      AND (key=? OR key LIKE ?) AND message_id IS NULL ORDER BY CASE WHEN key=? THEN 0 ELSE 1 END,key''',
                                      (guild.id, key, key + ':page:%', key))
    for legacy in publications:
        is_root = legacy['key'] == key
        channel = await service.channel(guild, legacy['channel_id'], discord.ForumChannel if is_root else None)
        found = await service.delivery.recover(guild, channel, legacy['key'], forum=is_root)
        if not found:
            raise ClubError('Прежняя тема или страница текущей книги не подтверждена. Проверьте /club diagnose и /club repair.')
        service.store.save_publication(legacy['key'], found[0].id, found[1].id)


async def _redirect_legacy_current(service, guild, forum_id, key, root_url, *, verify):
    # Never create a replacement for the superseded current topic. Its old
    # messages remain usable redirects; missing messages need no substitute.
    pubs = service.store.rows('SELECT * FROM bc_publications WHERE guild_id=? AND (key=? OR key LIKE ?)',
                              (guild.id, key, key + ':page:%'))
    content = '**Текущая книга — в закреплённой теме**\n' + f'[Открыть текущую книгу]({root_url})'
    view = _view(service, guild.id, [('Текущая книга', root_url)])
    signature = hashlib.sha256(repr((content, view.to_components())).encode()).hexdigest()
    for pub in pubs:
        if not pub['message_id']:
            continue
        identity = (pub['channel_id'], pub['message_id'], pub['content_hash'])
        if not verify and service.town_cache.get(pub['key']) == (signature, identity):
            continue
        try:
            thread = await service.channel(guild, pub['channel_id'], discord.Thread)
            if thread.parent_id != forum_id:
                continue  # Forum reconfiguration preserves the old history.
            message = await thread.fetch_message(pub['message_id'])
        except discord.NotFound:
            continue
        if not service.owns_starter(message):
            raise ClubError('Прежняя карточка текущей книги принадлежит другому автору.')
        if pub['content_hash'] != signature:
            # This obsolete navigation topic intentionally becomes an open
            # redirect; this never changes any book or essay archive state.
            if thread.archived:
                await thread.edit(archived=False)
            await message.edit(content=content, view=view, allowed_mentions=discord.AllowedMentions.none())
            service.store.save_publication(pub['key'], thread.id, message.id, signature)
        service.town_cache[pub['key']] = (signature, (thread.id, message.id, signature))


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
    await _recover_legacy_current(service, guild, key + ':current')
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
        guide = await service.paged_post(guild, key, forum_id, 'Текущая книга',
            ['**Текущая книга**\nПодготавливаем карточку…'],
            view=_current_view(service, guild.id, {}), inline_first=True)
        service.town_pin_checked.discard(guild.id)

    if not guide.get('managed_name'):
        # A pre-upgrade lost POST may have no remembered name yet. Only adopt
        # the original journaled title if Discord still shows that exact title;
        # any manual rename remains an explicit override.
        intent = service.delivery.intent(key)
        thread = await service.channel(guild, guide['channel_id'], discord.Thread)
        if intent and intent['forum_name'] and thread.name == intent['forum_name']:
            store.remember_publication_name(key, thread.name)

    old_guide_url = _url(guild.id, guide)
    sections = {}
    for purpose, title in TOPICS:
        # A saved edit can overtake us at each safe publication boundary.
        content = town_sections(store, guild.id)[purpose]
        sections[purpose] = await _publish(service, guild, f'{key}:{purpose}', forum_id, title, content,
            _view(service, guild.id, [('Текущая книга', old_guide_url)], actions=purpose == 'plan'), verify=verify)
        await service.book_updates.drain(guild)
    urls = {purpose: _url(guild.id, pub) for purpose, pub in sections.items()}
    guide = await _publish(service, guild, key, forum_id, pinned_title(store, guild.id), town_guide(store, guild.id, urls),
        _current_view(service, guild.id, urls), verify=verify)
    guide_url = _url(guild.id, guide)
    if guide_url != old_guide_url:
        # A deleted starter discovered by paged_post changes the root ID.
        # Repair all back links in this pass, preserving each section's ID.
        service.town_pin_checked.discard(guild.id)
        for purpose, title in TOPICS:
            sections[purpose] = await _publish(service, guild, f'{key}:{purpose}', forum_id, title, town_sections(store, guild.id)[purpose],
                _view(service, guild.id, [('Текущая книга', guide_url)], actions=purpose == 'plan'), verify=False)
        repaired_urls = {purpose: _url(guild.id, pub) for purpose, pub in sections.items()}
        if repaired_urls != urls:
            guide = await _publish(service, guild, key, forum_id, pinned_title(store, guild.id),
                town_guide(store, guild.id, repaired_urls),
                _current_view(service, guild.id, repaired_urls),
                verify=False)
    await _redirect_legacy_current(service, guild, forum_id, key + ':current', _url(guild.id, guide), verify=verify)
    await _pin_guide(service, guild, forum_id, guide, verify=verify)
    service.book_updates.failures.pop((guild.id, 'catalog'), None)
    return guide
