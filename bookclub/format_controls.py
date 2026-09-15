"""Persistent rules controls; every save checks membership and revision again."""
import discord
import json
import re

from .catalog_ui import CatalogGuardedView, CatalogGuardedModal, catalog_access, _reply, _same_guild
from .club_format import get_format, save_format
from .render import pages
from .store import ClubError


class FormatModal(CatalogGuardedModal):
    def __init__(self, service, guild_id, actor_id):
        super().__init__(title='Формат книжного клуба', timeout=600)
        self.service, self.guild_id, self.actor_id = service, guild_id, actor_id
        value = get_format(service.store, guild_id)
        self.revision = value['revision']
        self.body = discord.ui.TextInput(label='Общие правила', style=discord.TextStyle.paragraph,
                                         default=value['body'], max_length=1400)
        self.hours = discord.ui.TextInput(label='За сколько часов до обсуждения нужно эссе',
                                          default=str(value['essay_lead_hours']), max_length=3)
        self.add_item(self.body)
        self.add_item(self.hours)
        self.events = discord.ui.TextInput(label='События расписания: ссылки или ID', required=False,
            style=discord.TextStyle.paragraph, max_length=1000,
            default='\n'.join(f'https://discord.com/events/{guild_id}/{ident}' for ident in json.loads(value['schedule_ids'])))
        self.add_item(self.events)

    async def on_submit(self, interaction):
        _same_guild(interaction, self.guild_id)
        if interaction.user.id != self.actor_id:
            raise ClubError('Эта форма открыта другим организатором.')
        await interaction.response.defer(ephemeral=True)
        raw = self.hours.value.strip()
        if not raw.isascii() or not raw.isdecimal():
            raise ClubError('Введите целое число часов от 1 до 168.')
        ids = []
        for value in self.events.value.split():
            match = re.fullmatch(r'(?:https://discord.com/events/(\d+)/)?(\d+)', value)
            if not match or (match[1] and int(match[1]) != self.guild_id):
                raise ClubError('Расписание: ссылки на события этого сервера или их ID, по одному на строку.')
            ids.append(int(match[2]))
        async with self.service.locks[self.guild_id]:
            await catalog_access(self.service, interaction.guild, self.actor_id, organizer=True)
            events = await interaction.guild.fetch_scheduled_events()
            available = {e.id for e in events if e.guild_id == self.guild_id and e.entity_type == discord.EntityType.voice}
            if any(ident not in available for ident in ids):
                raise ClubError('Одно из событий недоступно или не является голосовой встречей этого сервера.')
            save_format(self.service.store, self.guild_id, self.actor_id,
                        self.body.value, int(raw), self.revision, ids)
            self.service.cache_schedule(interaction.guild, events)
            await self.service.refresh(interaction.guild)
        await _reply(interaction, 'Формат обновлён. Предыдущая версия и автор изменения сохранены в истории.')


class FormatView(CatalogGuardedView):
    def __init__(self, service, guild_id):
        super().__init__(timeout=None)
        self.service, self.guild_id = service, guild_id
        for action, label in [('edit', 'Изменить формат'), ('history', 'История изменений')]:
            button = discord.ui.Button(label=label, custom_id=f'bc:format:{guild_id}:{action}')
            async def callback(interaction, selected=action):
                _same_guild(interaction, guild_id)
                if selected == 'edit':
                    service.interaction_access(interaction)
                    if not service.interaction_organizer(interaction):
                        raise ClubError('Изменять формат может организатор клуба.')
                    await interaction.response.send_modal(FormatModal(service, guild_id, interaction.user.id))
                    return
                await interaction.response.defer(ephemeral=True)
                await catalog_access(service, interaction.guild, interaction.user.id, write=False)
                history = service.store.rows('SELECT * FROM bc_format_audit WHERE guild_id=? ORDER BY revision DESC', (guild_id,))
                view = FormatHistory(service, guild_id, interaction.user.id, history)
                await _reply(interaction, view.content(), view=view)
            button.callback = callback
            self.add_item(button)


class FormatHistory(CatalogGuardedView):
    def __init__(self, service, guild_id, actor_id, history):
        super().__init__(timeout=600)
        self.service, self.guild_id, self.actor_id = service, guild_id, actor_id
        self.index = 0
        self.entries = []
        for row in history:
            self.entries.extend(pages([
                f'**Версия {row["revision"]}** · <@{row["actor_id"]}> · <t:{row["created_at"]}:f>',
                f'Срок эссе: {row["old_hours"]} → {row["new_hours"]} ч до обсуждения.',
                'События до: ' + ', '.join(f'https://discord.com/events/{guild_id}/{ident}' for ident in json.loads(row['old_events'])),
                'События после: ' + ', '.join(f'https://discord.com/events/{guild_id}/{ident}' for ident in json.loads(row['new_events'])),
                '**До изменения**', row['old_body'], '**После изменения**', row['new_body'],
            ], limit=1700))
        self.entries = self.entries or ['Формат ещё не меняли. Используются исходные правила.']

    def content(self):
        self.previous.disabled = self.index == 0
        self.next_page.disabled = self.index == len(self.entries) - 1
        return self.entries[self.index] + f'\n\nСтраница {self.index + 1}/{len(self.entries)}'

    async def navigate(self, interaction, offset):
        _same_guild(interaction, self.guild_id)
        if interaction.user.id != self.actor_id:
            raise ClubError('Откройте свою историю изменений.')
        await interaction.response.defer(ephemeral=True)
        await catalog_access(self.service, interaction.guild, self.actor_id, write=False)
        self.index = max(0, min(len(self.entries) - 1, self.index + offset))
        await interaction.edit_original_response(content=self.content(), view=self,
                                                  allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label='Назад')
    async def previous(self, interaction, button):
        await self.navigate(interaction, -1)

    @discord.ui.button(label='Далее')
    async def next_page(self, interaction, button):
        await self.navigate(interaction, 1)
