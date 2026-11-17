"""Shared whiteboards cog: /board posts a fresh ExcaliDash board anyone can edit.

Inert until an administrator runs /board_setup. The recorder asks
board_for_meeting() for a board when a recording starts; every failure here is
reported as text and never breaks the recorder or the shuffle bot.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .client import BoardError, ExcaliDash, normalize_url
from .store import BoardStore, board_message, default_name, list_message

log = logging.getLogger("reshuffle.board")
NOT_CONFIGURED = "Доска пока не подключена. Администратор сервера может подключить её командой /board_setup."
USER_COOLDOWN = 30


class Boards(commands.Cog):
    def __init__(self, bot: commands.Bot, data_dir: str | Path, *, client_factory=ExcaliDash):
        self.bot = bot
        self.store = BoardStore(Path(data_dir) / "board.json")
        self.client_factory = client_factory
        self._client: ExcaliDash | None = None
        self._client_key: tuple | None = None
        self._last_use: dict[int, float] = {}

    async def cog_unload(self) -> None:
        await self._drop_client()

    # ---------- helpers ----------
    async def _drop_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        self._client, self._client_key = None, None

    async def client(self) -> ExcaliDash:
        if not self.store.configured:
            raise BoardError(NOT_CONFIGURED)
        key = self.store.credentials()
        if self._client is None or self._client_key != key:
            await self._drop_client()
            self._client = self.client_factory(*key)
            self._client_key = key
        return self._client

    def now(self) -> datetime:
        return datetime.now(self.store.timezone)

    async def new_board(self, name: str | None, channel, *, meeting: bool = False) -> dict:
        client = await self.client()
        title = (name or "").strip() or default_name(getattr(channel, "name", None), self.now())
        board = await client.create_board(title)
        guild = getattr(channel, "guild", None)
        entry = self.store.remember(board, guild_id=getattr(guild, "id", None),
                                    channel_id=getattr(channel, "id", None), meeting=meeting)
        log.info("board %s created for #%s", board["id"], getattr(channel, "name", "?"))
        return entry

    async def board_for_meeting(self, channel) -> dict | None:
        """Board for a recording that just started; None when boards are off or unavailable."""
        if not self.store.meeting_boards:
            return None
        reused = self.store.meeting_board(channel.id)
        if reused:
            return reused
        try:
            return await self.new_board(None, channel, meeting=True)
        except BoardError as exc:
            log.warning("no board for the meeting in #%s: %s", getattr(channel, "name", "?"), exc)
        except Exception:
            log.exception("no board for the meeting in #%s", getattr(channel, "name", "?"))
        return None

    def meeting_message(self, board: dict) -> str:
        return board_message(board, self.store.timezone, meeting=True)

    # ---------- commands for everyone ----------
    @app_commands.command(name="board", description="Создать общую доску для рисования и выложить ссылку в канал")
    @app_commands.describe(name="Название доски (по умолчанию — канал и время)")
    @app_commands.guild_only()
    async def board(self, interaction: discord.Interaction, name: str | None = None) -> None:
        if not self.store.configured:
            await interaction.response.send_message(NOT_CONFIGURED, ephemeral=True)
            return
        user_id = interaction.user.id
        wait = USER_COOLDOWN - (time.monotonic() - self._last_use.get(user_id, -USER_COOLDOWN))
        if wait > 0:
            await interaction.response.send_message(
                f"Новую доску можно создать через {int(wait) + 1} с.", ephemeral=True)
            return
        self._last_use[user_id] = time.monotonic()
        await interaction.response.defer(thinking=True)
        try:
            entry = await self.new_board(name, interaction.channel)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, BoardError) else "внутренняя ошибка бота"
            if not isinstance(exc, BoardError):
                log.exception("/board failed")
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
            await interaction.followup.send(f"Не удалось создать доску: {reason}", ephemeral=True)
            return
        await interaction.followup.send(board_message(entry, self.store.timezone))

    @app_commands.command(name="board_list", description="Последние доски с действующими ссылками")
    @app_commands.guild_only()
    async def board_list(self, interaction: discord.Interaction) -> None:
        if not self.store.configured:
            await interaction.response.send_message(NOT_CONFIGURED, ephemeral=True)
            return
        boards = self.store.recent(interaction.guild_id, limit=10)
        await interaction.response.send_message(list_message(boards, self.store.timezone), ephemeral=True)

    # ---------- admin commands ----------
    @app_commands.command(name="board_setup", description="Подключить общую доску ExcaliDash (адрес и вход для бота)")
    @app_commands.describe(url="Адрес доски, например https://…:8443",
                           login="Логин или почта учётной записи бота на доске",
                           password="Пароль этой учётной записи",
                           meeting_boards="Выкладывать доску, когда начинается запись созвона")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def board_setup(self, interaction: discord.Interaction, url: str, login: str = "",
                          password: str = "", meeting_boards: bool = True) -> None:
        try:
            clean_url = normalize_url(url)
        except BoardError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.store.configure(clean_url, login, password, meeting_boards)
        await self._drop_client()
        try:
            info = await (await self.client()).check()
            result = f"проверка прошла, досок у учётной записи: {info.get('boards', '?')}"
        except BoardError as exc:
            result = f"но проверить не удалось: {exc}"
        except Exception:
            log.exception("board check failed")
            result = "но проверить не удалось: внутренняя ошибка бота"
        meetings = "к записям созвонов доска будет выкладываться" if meeting_boards else \
            "к записям созвонов доска не выкладывается"
        await interaction.followup.send(f"Доска сохранена ({clean_url}), {result}. Сейчас {meetings}.",
                                        ephemeral=True)

    @app_commands.command(name="board_status", description="Подключена ли общая доска и отвечает ли она")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def board_status(self, interaction: discord.Interaction) -> None:
        status = self.store.public_status()
        if not status["url"]:
            await interaction.response.send_message(NOT_CONFIGURED, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            info = await (await self.client()).check()
            health = f"отвечает, вход бота работает (досок: {info.get('boards', '?')})"
        except BoardError as exc:
            health = f"не работает: {exc}"
        except Exception:
            log.exception("board status check failed")
            health = "не работает: внутренняя ошибка бота"
        login = status["login"] or "не задан"
        lines = [f"Адрес: {status['url']}",
                 f"Учётная запись бота: {login}, пароль {'задан' if status['password_set'] else 'не задан'}",
                 f"Доска: {health}",
                 f"Доска к записи созвонов: {'да' if status['meeting_boards'] else 'нет'}",
                 f"Досок создано ботом (в истории): {status['boards']}"]
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="board_off", description="Отключить общую доску (забыть адрес и пароль)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def board_off(self, interaction: discord.Interaction) -> None:
        self.store.clear()
        await self._drop_client()
        await interaction.response.send_message("Доска отключена: адрес и пароль удалены.", ephemeral=True)


async def setup_boards(bot: commands.Bot, data_dir: str | Path) -> None:
    await bot.add_cog(Boards(bot, data_dir))
