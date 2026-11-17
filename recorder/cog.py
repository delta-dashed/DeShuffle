"""Meeting recorder cog: joins configured voice channels and records each speaker.

Inert until an administrator enables a channel with /record_channel_add. While
recording it posts a notice in the channel chat. After everyone leaves it mixes
the tracks, writes meta.json (who was there, how long each person spoke) and,
if an upload URL is configured, POSTs the recording there.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from .core import Session, patch_voice_recv_for_dave

log = logging.getLogger("reshuffle.recorder")
NOTICE = "🔴 Идёт запись созвона для итогов встречи. Бот выйдет сам, когда все разойдутся."
EMPTY_GRACE = 60
MIN_HUMANS = 2
KEEP_SESSIONS = 5


def humans(channel) -> list:
    return [member for member in channel.members if not member.bot]


class Recorder(commands.Cog):
    def __init__(self, bot: commands.Bot, data_dir: str | Path):
        self.bot = bot
        self.root = Path(data_dir) / "recordings"
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_file = Path(data_dir) / "recorder.json"
        self.config = self._load()
        self.sessions: dict[int, Session] = {}
        self.watcher: asyncio.Task | None = None

    # ---------- config ----------
    def _load(self) -> dict:
        try:
            return json.loads(self.config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"channels": [], "upload_url": "", "upload_token": ""}

    def _save(self) -> None:
        tmp = self.config_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.config_file)

    def watched(self, channel) -> bool:
        return channel is not None and channel.id in self.config.get("channels", [])

    # ---------- lifecycle ----------
    async def cog_load(self) -> None:
        self.watcher = asyncio.create_task(self._watch())

    async def cog_unload(self) -> None:
        if self.watcher:
            self.watcher.cancel()
        for channel_id in list(self.sessions):
            await self.stop(channel_id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            for channel in guild.voice_channels:
                if self.watched(channel) and len(humans(channel)) >= MIN_HUMANS and channel.id not in self.sessions:
                    await self.start(channel)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        if member.bot:
            return
        for channel in {before.channel, after.channel} - {None}:
            if not self.watched(channel):
                continue
            session = self.sessions.get(channel.id)
            if session:
                if after.channel and after.channel.id == channel.id:
                    session.seen(member.id, member.display_name)
                elif before.channel and before.channel.id == channel.id:
                    session.gone(member.id)
            elif len(humans(channel)) >= MIN_HUMANS:
                await self.start(channel)

    async def start(self, channel) -> None:
        try:
            from discord.ext import voice_recv
        except ImportError:
            log.error("discord-ext-voice-recv is not installed; recording disabled")
            return
        patch_voice_recv_for_dave()
        session = Session(channel.id, channel.name, channel.guild.name, self.root)
        self.sessions[channel.id] = session
        try:
            await self._connect(session, channel)
            for member in humans(channel):
                session.seen(member.id, member.display_name)
            log.info("recording started in %s", channel.name)
            try:
                await channel.send(NOTICE)
            except discord.HTTPException:
                pass
        except Exception:
            log.exception("could not join %s", channel.name)
            self.sessions.pop(channel.id, None)
            return
        await self._attach_board(session, channel)

    async def _attach_board(self, session: Session, channel) -> None:
        """Post a shared board for this meeting when the board cog is set up (optional)."""
        boards = self.bot.get_cog("Boards")
        if boards is None:
            return
        try:
            board = await asyncio.wait_for(boards.board_for_meeting(channel), timeout=45)
        except Exception:
            log.exception("meeting board for %s", channel.name)
            return
        if not board:
            return
        session.board = board
        try:
            await channel.send(boards.meeting_message(board))
        except discord.HTTPException:
            log.warning("could not post the meeting board in %s", channel.name)

    async def _connect(self, session: Session, channel) -> None:
        from discord.ext import voice_recv

        class Sink(voice_recv.AudioSink):
            def wants_opus(self) -> bool:
                return False

            def write(self, user, data) -> None:
                if user is None or getattr(user, "bot", False) or not data.pcm:
                    return
                session.feed(user.id, getattr(user, "display_name", str(user)), data.pcm)

            def cleanup(self) -> None:
                pass

        session.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient, self_mute=True)
        session.voice_client.listen(Sink())

    async def stop(self, channel_id: int) -> None:
        session = self.sessions.pop(channel_id, None)
        if not session:
            return
        try:
            if session.voice_client:
                session.voice_client.stop_listening()
                await session.voice_client.disconnect(force=True)
        except Exception:
            log.exception("leaving voice channel")
        mix = await asyncio.get_running_loop().run_in_executor(None, session.finish)
        if mix:
            await self._upload(session, mix)
        self._prune()

    async def _upload(self, session: Session, mix: Path) -> None:
        url = self.config.get("upload_url")
        if not url:
            log.info("recording kept locally: %s", session.dir)
            return
        headers = {"Authorization": f"Bearer {self.config.get('upload_token', '')}"}
        for attempt in range(5):
            try:
                form = aiohttp.FormData()
                form.add_field("meta", (session.dir / "meta.json").read_text(encoding="utf-8"),
                               content_type="application/json")
                form.add_field("audio", mix.read_bytes(), filename=f"{session.stamp} Discord {session.channel_name}.m4a",
                               content_type="audio/mp4")
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as http:
                    async with http.post(url, data=form, headers=headers) as response:
                        if response.status < 300:
                            (session.dir / "uploaded").write_text(str(int(time.time())))
                            log.info("recording uploaded: %s", session.dir.name)
                            return
                        log.warning("upload answered %s", response.status)
            except Exception as exc:
                log.warning("upload failed: %s", exc)
            await asyncio.sleep(30 * (attempt + 1))

    def _prune(self) -> None:
        sessions = sorted(p for p in self.root.iterdir() if p.is_dir())
        for old in sessions[:-KEEP_SESSIONS]:
            if (old / "uploaded").exists() or not self.config.get("upload_url"):
                shutil.rmtree(old, ignore_errors=True)

    async def _watch(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(10)
            for channel_id, session in list(self.sessions.items()):
                channel = self.bot.get_channel(channel_id)
                if channel is None or not humans(channel):
                    session.empty_since = session.empty_since or time.time()
                    if time.time() - session.empty_since >= EMPTY_GRACE:
                        await self.stop(channel_id)
                    continue
                session.empty_since = None
                if session.voice_client and not session.voice_client.is_connected():
                    try:
                        await self._connect(session, channel)
                    except Exception:
                        log.exception("reconnect to %s", channel.name)

    # ---------- admin commands ----------
    @app_commands.command(name="record_channel_add", description="Записывать созвоны в этом голосовом канале")
    @app_commands.default_permissions(manage_guild=True)
    async def record_channel_add(self, interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
        channels = self.config.setdefault("channels", [])
        if channel.id not in channels:
            channels.append(channel.id)
            self._save()
        await interaction.response.send_message(f"Запись включена: {channel.mention}", ephemeral=True)

    @app_commands.command(name="record_channel_remove", description="Перестать записывать этот голосовой канал")
    @app_commands.default_permissions(manage_guild=True)
    async def record_channel_remove(self, interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
        channels = self.config.setdefault("channels", [])
        if channel.id in channels:
            channels.remove(channel.id)
            self._save()
        await self.stop(channel.id)
        await interaction.response.send_message(f"Запись выключена: {channel.mention}", ephemeral=True)

    @app_commands.command(name="record_status", description="Какие каналы записываются и куда уходят записи")
    @app_commands.default_permissions(manage_guild=True)
    async def record_status(self, interaction: discord.Interaction) -> None:
        names = [f"<#{c}>" for c in self.config.get("channels", [])] or ["нет"]
        live = [f"<#{c}>" for c in self.sessions] or ["нет"]
        target = "задан" if self.config.get("upload_url") else "не задан (записи остаются на хосте бота)"
        await interaction.response.send_message(
            f"Каналы: {', '.join(names)}\nИдёт запись: {', '.join(live)}\nАдрес приёма: {target}", ephemeral=True)

    @app_commands.command(name="record_upload_url", description="Куда отправлять готовые записи")
    @app_commands.default_permissions(manage_guild=True)
    async def record_upload_url(self, interaction: discord.Interaction, url: str, token: str = "") -> None:
        self.config["upload_url"] = url.strip()
        self.config["upload_token"] = token.strip()
        self._save()
        await interaction.response.send_message("Адрес приёма записей сохранён.", ephemeral=True)


async def setup_recorder(bot: commands.Bot, data_dir: str | Path) -> None:
    await bot.add_cog(Recorder(bot, data_dir))
